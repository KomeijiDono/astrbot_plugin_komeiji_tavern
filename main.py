from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .constants import DESCRIPTION, DISPLAY_NAME, PLUGIN_ID, PLUGIN_VERSION
from .illustration import OmniDrawBridge
from .service import TavernService
from .qq_delivery import split_forward_text
from .storage import TavernStorage
from .web import TavernWebApi


_STATE_JSON = re.compile(r"\[TAVERN_STATE\]\s*(\{.*?\})\s*$", re.DOTALL)
_STATE_FIELDS = re.compile(r"\[LOVE_DATA\]\s*(.+)$", re.MULTILINE)
_CONFIG_GROUPS = (
    "basic_config", "context_config", "worldbook_config", "qq_direct_config",
    "qq_forward_config", "status_config", "illustration_config",
)


def _flatten_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Accept both legacy flat config and Dashboard grouped config."""
    flattened = dict(config or {})
    for group in _CONFIG_GROUPS:
        values = flattened.get(group)
        if isinstance(values, dict):
            flattened.update(values)
    return flattened


@register(PLUGIN_ID, "KomeijiDono", DESCRIPTION, PLUGIN_VERSION)
class KomeijiTavernPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = _flatten_config(config)
        data_dir = Path.home() / ".astrbot" / "data" / PLUGIN_ID
        self.storage = TavernStorage(data_dir / "tavern.db")
        self.service = TavernService(self.storage, context, self.config)
        self.web = TavernWebApi(self.storage, self.service, context, Path(__file__).parent / "web" / "dist")
        self.illustration = OmniDrawBridge(context, self.config)

    async def initialize(self) -> None:
        self.service.ensure_defaults()
        for path, methods, handler, description in self.web.routes():
            self.context.register_web_api(path, handler, methods, description)
        logger.info("[%s] initialized", DISPLAY_NAME)

    async def terminate(self) -> None:
        await self.illustration.terminate()

    @staticmethod
    def _session_id(event: AstrMessageEvent, req: ProviderRequest | None = None) -> str:
        return str(event.unified_msg_origin or (req.session_id if req else "") or "default")

    @filter.on_llm_request(priority=-1000)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        result = await self.service.process(event, req)
        req.system_prompt = result.system_prompt
        req.contexts = result.contexts

        if self.config.get("tool_delivery_enabled", False) and req.func_tool:
            tool = req.func_tool.get_func("send_message_to_user")
            if tool is not None:
                event.set_extra("_kt_tool", tool)
                event.set_extra("_kt_tool_description", tool.description)
                tool.description = (
                    "Send the completed roleplay reply to the user. Put the visible narrative in "
                    "this tool call instead of returning it as ordinary assistant content."
                )

    @filter.on_llm_response(priority=-1000)
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        tool = event.get_extra("_kt_tool")
        original = event.get_extra("_kt_tool_description")
        if tool is not None and original is not None:
            tool.description = original

        try:
            if not self.config.get("status_bar_enabled", False):
                return
            text = response.completion_text or ""
            state_match = _STATE_JSON.search(text)
            fields_match = _STATE_FIELDS.search(text)
            if not state_match and not fields_match:
                return
            session_id = self._session_id(event)
            async with self.service._session_lock(session_id):
                state = await asyncio.to_thread(self.storage.get_session, session_id)
                variables = state.setdefault("variables", {})
                status_content = ""
                if state_match:
                    try:
                        payload = json.loads(state_match.group(1))
                        if isinstance(payload, dict):
                            variables.update({str(key): value for key, value in payload.items()})
                            status_content = " | ".join(f"{key}: {value}" for key, value in payload.items())
                        response.completion_text = text[:state_match.start()].rstrip()
                    except json.JSONDecodeError:
                        logger.warning("[%s] invalid state payload", DISPLAY_NAME)
                elif fields_match:
                    parts = [part.strip() for part in fields_match.group(1).split("|")]
                    variables.update({f"state_{index + 1}": value for index, value in enumerate(parts)})
                    status_content = " | ".join(parts)
                    response.completion_text = text[:fields_match.start()].rstrip()
                if status_content:
                    template = str(self.config.get("status_bar_template", "**Status**\n```\n{content}\n```"))
                    response.completion_text = (response.completion_text or "").rstrip() + "\n\n" + template.replace("{content}", status_content)
                await asyncio.to_thread(self.storage.save_session, session_id, state)
        finally:
            if self.config.get("illustration_enabled", False):
                event.set_extra("_kt_illustration_text", str(response.completion_text or ""))

    async def _dispatch_pending_illustration(self, event: AstrMessageEvent) -> None:
        get_extra = getattr(event, "get_extra", None)
        text = str(get_extra("_kt_illustration_text") or "") if callable(get_extra) else ""
        if not text:
            return
        event.set_extra("_kt_illustration_text", "")
        await self.illustration.maybe_illustrate_text(event, text)

    @filter.after_message_sent(priority=1000)
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        await self._dispatch_pending_illustration(event)

    async def _send_qq_direct_chunks(
        self,
        event: AstrMessageEvent,
        chunks: list[str],
        *,
        start_index: int = 0,
        total_chunks: int | None = None,
    ) -> None:
        interval_ms = max(0, int(self.config.get("qq_direct_send_interval_ms", 2000)))
        retry_count = max(0, int(self.config.get("qq_direct_retry_count", 2)))
        retry_delay_ms = max(0, int(self.config.get("qq_direct_retry_delay_ms", 3000)))
        total = total_chunks if total_chunks is not None else start_index + len(chunks)
        for offset, chunk in enumerate(chunks):
            index = start_index + offset
            retry = 0
            while True:
                try:
                    await event.send(MessageChain([Plain(chunk)]))
                    logger.info(
                        "[%s] QQ 普通消息分片 %d/%d 发送成功（%d 字符）",
                        DISPLAY_NAME,
                        index + 1,
                        total,
                        len(chunk),
                    )
                    break
                except Exception as exc:
                    if retry >= retry_count:
                        logger.error(
                            "[%s] QQ 普通消息分片 %d/%d 发送失败，已用尽 %d 次重试：%s",
                            DISPLAY_NAME,
                            index + 1,
                            total,
                            retry_count,
                            exc,
                        )
                        raise
                    retry += 1
                    logger.warning(
                        "[%s] QQ 普通消息分片 %d/%d 发送失败，%dms 后进行第 %d/%d 次重试：%s",
                        DISPLAY_NAME,
                        index + 1,
                        total,
                        retry_delay_ms,
                        retry,
                        retry_count,
                        exc,
                    )
                    if retry_delay_ms:
                        await asyncio.sleep(retry_delay_ms / 1000)
            if interval_ms and offset + 1 < len(chunks):
                await asyncio.sleep(interval_ms / 1000)

    @filter.on_decorating_result(priority=-1000)
    async def deliver_qq_long_reply(self, event: AstrMessageEvent):
        """Deliver long plain-text QQ replies as direct chunks or forward nodes."""
        if event.get_platform_name() != "aiocqhttp":
            return
        result = event.get_result()
        get_extra = getattr(event, "get_extra", None)
        force_long_delivery = bool(get_extra("_kt_force_long_delivery")) if callable(get_extra) else False
        if result is None or (not result.is_llm_result() and not force_long_delivery) or not result.chain:
            return
        if not all(isinstance(component, Plain) for component in result.chain):
            return

        text = "".join(component.text for component in result.chain)
        trigger = max(100, int(self.config.get("qq_forward_trigger_chars", 1500)))
        if len(text) <= trigger:
            return

        if self.config.get("qq_direct_split_enabled", False):
            message_chars = max(100, int(self.config.get("qq_direct_message_chars", 1000)))
            chunks = split_forward_text(text, message_chars)
            event.clear_result()
            await self._send_qq_direct_chunks(event, chunks)
            logger.info(
                "[%s] QQ 长回复已按每条最多 %d 字符直接发送为 %d 条消息（共 %d 字符）",
                DISPLAY_NAME,
                message_chars,
                len(chunks),
                len(text),
            )
            await self._dispatch_pending_illustration(event)
            return

        if not self.config.get("qq_forward_split_enabled", True):
            return
        node_chars = max(100, int(self.config.get("qq_forward_node_chars", 1000)))
        nodes_per_batch = max(1, int(self.config.get("qq_forward_nodes_per_batch", 6)))
        batch_interval_ms = max(0, int(self.config.get("qq_forward_batch_interval_ms", 1500)))
        fallback_enabled = bool(self.config.get("qq_forward_fallback_enabled", True))
        chunks = split_forward_text(text, node_chars)
        event.clear_result()
        batches = [chunks[index:index + nodes_per_batch] for index in range(0, len(chunks), nodes_per_batch)]
        sent_chunks = 0
        for batch_index, batch in enumerate(batches):
            nodes = [
                Node(uin=event.get_self_id(), name="AstrBot", content=[Plain(chunk)])
                for chunk in batch
            ]
            try:
                await event.send(MessageChain([Nodes(nodes)]))
                sent_chunks += len(batch)
                logger.info(
                    "[%s] QQ 合并转发包 %d/%d 发送成功（%d 个 Node，%d 字符）",
                    DISPLAY_NAME,
                    batch_index + 1,
                    len(batches),
                    len(nodes),
                    sum(len(chunk) for chunk in batch),
                )
            except Exception as exc:
                if not fallback_enabled:
                    logger.error("[%s] QQ 合并转发发送失败且未启用自动降级：%s", DISPLAY_NAME, exc)
                    raise
                remaining = chunks[sent_chunks:]
                logger.warning(
                    "[%s] QQ 合并转发包 %d/%d 发送失败，将剩余 %d 个分片降级为普通消息：%s",
                    DISPLAY_NAME,
                    batch_index + 1,
                    len(batches),
                    len(remaining),
                    exc,
                )
                await self._send_qq_direct_chunks(
                    event,
                    remaining,
                    start_index=sent_chunks,
                    total_chunks=len(chunks),
                )
                await self._dispatch_pending_illustration(event)
                return
            if batch_interval_ms and batch_index + 1 < len(batches):
                await asyncio.sleep(batch_interval_ms / 1000)
        await self._dispatch_pending_illustration(event)

    @filter.command("tavern")
    async def tavern(self, event: AstrMessageEvent, action: str = "status", rest: GreedyStr = ""):
        """Komeiji's Tavern: status, preview, reset, continue, impersonate, quiet."""
        action = (action or "status").strip().lower()
        session_id = self._session_id(event)
        if action == "preview":
            preview = await asyncio.to_thread(self.storage.get_preview, session_id)
            event.set_extra("_kt_force_long_delivery", True)
            yield event.plain_result(json.dumps(preview or {}, ensure_ascii=False, indent=2))
            return
        if action == "reset":
            await self.service.reset_session(session_id)
            yield event.plain_result("当前会话的世界书生命周期和预览状态已清除。")
            return
        if action == "status":
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            yield event.plain_result(
                f"{DISPLAY_NAME} {PLUGIN_VERSION}\n会话：{session_id}\n轮次：{state.get('turn', 0)}\n"
                f"生命周期记录：{len(state.get('effects', {}))}\n可在插件管理页查看绑定和最终 messages[]。"
            )
            return
        if action not in {"continue", "impersonate", "quiet"}:
            yield event.plain_result("用法：/tavern status|preview|reset|continue|impersonate|quiet [补充提示]")
            return
        event.set_extra("_kt_mode", action)
        event.set_extra("_kt_quiet_prompt", str(rest) if action == "quiet" else "")
        manager = self.context.conversation_manager
        conversation_id = await manager.get_curr_conversation_id(event.unified_msg_origin)
        conversation = await manager.get_conversation(event.unified_msg_origin, conversation_id) if conversation_id else None
        prompt = str(rest).strip() or {
            "continue": "Continue.", "impersonate": "Draft my next message.", "quiet": "Generate quietly."
        }[action]
        yield event.request_llm(prompt=prompt, conversation=conversation)
