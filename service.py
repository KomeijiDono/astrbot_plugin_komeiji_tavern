from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import random
import json
import time
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet

from .cipher import CipherCodec
from .constants import PLUGIN_VERSION
from .lore import LoreScanner, normalize_entries
from .models import BuildResult, LoreEntry, ScanResult
from .prompt_builder import PromptBuilder, estimate_tokens
from .storage import TavernStorage
from .url_request import (
    FETCH_TOOL_NAME,
    SUBMIT_REPLY_TOOL_NAME,
    URLRequestBroker,
    URLRequestHandle,
    URLRequestProtocolError,
    build_fetch_tool,
    build_submit_reply_tool,
    request_url_user_prompt,
    strip_reasoning_tags,
    url_request_overhead_text,
)

PLUGIN_TAG = "[Komeiji's Tavern]"
DEFAULT_MEMORY_PROMPT = """从以下角色扮演聊天中提取需要长期保留的记忆。
只提取对后续跨会话继续 RP 有价值的信息，例如用户偏好、角色关系变化、重要剧情节点、长期状态。
不要续写剧情，不要加入原文没有的信息。
请只输出 JSON 数组，每项格式为 {"category":"preference|relationship|plot|status","content":"一句具体记忆"}。
如果没有值得保存的内容，输出 []。

聊天记录：
{history}
"""
DEFAULT_SUMMARY_PROMPT = """请把以下旧聊天记录压缩成可供后续角色扮演继续使用的会话摘要。
保留人物关系、重要事实、事件顺序、承诺、状态变化、未解决事项和持续有效的偏好。
不要续写剧情，不要添加原文没有的信息，不要输出标题之外的解释。

已有摘要：
{previous_summary}

新增旧聊天记录：
{history}
"""

DEFAULT_CAMPAIGN_STATE_PROMPT = """你是角色扮演战役的状态记录员。根据本轮玩家输入与叙事结果，提出对当前结构化状态的最小修改。
只记录文本明确发生的变化；不要把计划、猜测、选项或修辞当成事实。不要自行补全数值。
仅输出 JSON 对象：{"patch":[{"op":"set|delete|increment|append|remove","path":"点号分隔路径","value":任意JSON值}],"reason":"简短依据"}。
若没有可靠变化，输出 {"patch":[],"reason":""}。

状态字段说明：
{schema}

当前状态：
{state}

玩家输入：
{user}

叙事结果：
{assistant}
"""

STATE_TEMPLATES: dict[str, dict[str, Any]] = {
    "survival": {
        "name": "生存探索",
        "schema": {
            "time": "日期、时间与天气", "location": "当前精确位置",
            "condition": "体力、饥渴、伤势与感染", "system": "积分与等级",
            "resources": "食物、水、燃料、弹药、药品与建材的准确数量",
            "key_items": "钥匙、文件、图纸和任务道具", "equipment": "武器、护具与工具",
            "quests": "active/completed/failed/warnings", "base": "据点设施与安全状态",
            "clues": "已经确认的情报", "relationships": "NPC关系、信任与承诺",
        },
        "state": {
            "time": {"day": "开局", "clock": "未知", "weather": "未知"}, "location": "开局地点",
            "condition": {"stamina_percent": 100, "hunger": "正常", "thirst": "正常", "injuries": [], "infection": "无"},
            "system": {"points": 0, "level": 1}, "resources": {}, "key_items": [], "equipment": [],
            "quests": {"active": [], "completed": [], "failed": [], "warnings": []},
            "base": None, "clues": [], "relationships": {},
        },
    },
    "light": {
        "name": "轻量叙事",
        "schema": {"time": "当前时间", "location": "当前位置", "quests": "目标与未解决事项", "clues": "确认事实", "relationships": "人物关系"},
        "state": {"time": "开局", "location": "开局地点", "quests": {"active": [], "completed": []}, "clues": [], "relationships": {}},
    },
    "trpg": {
        "name": "TRPG",
        "schema": {"scene": "当前场景", "attributes": "角色属性", "hp": "生命值", "mp": "资源值", "effects": "持续效果", "inventory": "物品", "quests": "任务", "clues": "线索"},
        "state": {"scene": "开场", "attributes": {}, "hp": {"current": 100, "max": 100}, "mp": {"current": 0, "max": 0}, "effects": [], "inventory": [], "quests": [], "clues": []},
    },
    "relationship": {
        "name": "关系叙事",
        "schema": {"time": "时间", "location": "地点", "mood": "当前情绪", "relationships": "关系阶段、信任与承诺", "events": "关键事件", "open_threads": "未解决事项"},
        "state": {"time": "开局", "location": "开局地点", "mood": {}, "relationships": {}, "events": [], "open_threads": []},
    },
}

DEFAULT_QUICK_REPLIES = [
    {"id": "default-continue", "label": "继续剧情", "alias": "continue", "content": "继续上一条助手回复，从中断处自然衔接。推进当前场景，避免复述已有内容，也不要替用户决定行动或台词。", "mode": "continue", "enabled": True, "append_input": True, "order": 10},
    {"id": "default-detail", "label": "丰富描写", "alias": "detail", "content": "结合当前上下文继续回应，并加强动作、神态、环境与感官细节。保持人物性格和既有设定，不要无故改变剧情事实。", "mode": "normal", "enabled": True, "append_input": True, "order": 20},
    {"id": "default-impersonate", "label": "代写我的回复", "alias": "reply", "content": "根据当前对话和用户设定，拟写一条自然的下一步用户消息。保持用户的口吻，只输出可直接发送的消息正文。", "mode": "impersonate", "enabled": True, "append_input": True, "order": 30},
    {"id": "default-polish", "label": "润色重写", "alias": "polish", "content": "润色并重写上一条助手回复，保留原意和事实，提高语言自然度、画面感与节奏。只输出重写后的正文。", "mode": "normal", "enabled": True, "append_input": True, "order": 40},
    {"id": "default-summary", "label": "总结当前剧情", "alias": "summary", "content": "总结截至目前的剧情进展、人物关系、重要信息、当前状态和未解决事项。不要续写剧情，不要添加上下文中不存在的信息。", "mode": "normal", "enabled": True, "append_input": True, "order": 50},
    {"id": "default-in-character", "label": "严格保持角色", "alias": "incharacter", "content": "本轮必须严格遵守角色卡、世界书与既有剧情事实，保持角色口吻和行为逻辑，不要跳出角色解释。", "mode": "quiet", "enabled": True, "append_input": True, "order": 60},
]


class TavernService:
    def __init__(self, storage: TavernStorage, context: Any, config: dict[str, Any]):
        self.storage = storage
        self.context = context
        self.config = config
        self.cipher = CipherCodec(str(config.get("cipher_method", "base64_utf8")))
        url_request_enabled = bool(config.get("url_request_enabled", False))
        cipher_enabled = (
            bool(config.get("cipher_enabled", False))
            and not url_request_enabled
        )
        self.url_requests = URLRequestBroker(
            enabled=url_request_enabled,
            backend=str(config.get("url_request_backend", "local") or "local"),
            public_base_url=str(
                config.get("url_request_public_base_url", "") or ""
            ),
            listen_host=str(
                config.get("url_request_listen_host", "0.0.0.0") or "0.0.0.0"
            ),
            listen_port=int(config.get("url_request_listen_port", 6190) or 6190),
            ttl_seconds=int(
                config.get("url_request_ttl_seconds", 600) or 600
            ),
            fetch_tool_enabled=bool(
                config.get("url_request_fetch_tool_enabled", True)
            ),
            reply_tool_enabled=bool(
                config.get("url_request_reply_tool_enabled", True)
            ),
            plugin_version=PLUGIN_VERSION,
            hosted_api_base_url=str(
                config.get("url_request_hosted_api_base_url", "") or ""
            ),
            hosted_api_key=str(
                config.get("url_request_hosted_api_key", "") or ""
            ),
            hosted_timeout_seconds=int(
                config.get("url_request_hosted_timeout_seconds", 10) or 10
            ),
            hosted_proxy_url=str(
                config.get("url_request_hosted_proxy_url", "") or ""
            ),
        )
        self.scanner = LoreScanner(
            default_scan_depth=int(config.get("scan_depth", 4)),
            max_recursion_steps=int(config.get("max_recursion_steps", 3)),
        )
        self.builder = PromptBuilder(
            context_budget=int(config.get("context_budget", 32768)),
            output_reserve=int(config.get("output_reserve", 2048)),
            history_first_trimming=bool(config.get("history_first_trimming", True)),
            history_keep_recent_messages=int(config.get("history_keep_recent_messages", 6)),
            history_max_messages=int(config.get("history_max_messages", 12)),
            content_token_estimator=(
                lambda text: estimate_tokens(self.cipher.encode_payload(text))
                if cipher_enabled else estimate_tokens(text)
            ),
            request_overhead_tokens=(
                estimate_tokens(
                    url_request_overhead_text(
                        fetch_tool_enabled=self.url_requests.fetch_tool_enabled,
                        reply_tool_enabled=self.url_requests.reply_tool_enabled,
                    )
                )
                if url_request_enabled
                else estimate_tokens(self.cipher.protocol_prompt)
                if cipher_enabled
                else 0
            ),
            message_overhead_tokens=(
                estimate_tokens(self.cipher.encode_content("")) if cipher_enabled else 0
            ),
        )
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_limit = 512
        self._session_locks: dict[str, asyncio.Lock] = {}

    @property
    def cipher_enabled(self) -> bool:
        return bool(self.config.get("cipher_enabled", False)) and not self.url_request_enabled

    @property
    def cipher_configured(self) -> bool:
        return bool(self.config.get("cipher_enabled", False))

    @property
    def url_request_enabled(self) -> bool:
        return bool(self.config.get("url_request_enabled", False))

    def provider_messages(self, result: BuildResult) -> list[dict[str, Any]]:
        messages = copy.deepcopy(result.messages)
        return self.cipher.encode_messages(messages) if self.cipher_enabled else messages

    def provider_request_parts(
        self, result: BuildResult,
    ) -> tuple[str, list[dict[str, Any]], str]:
        if not self.cipher_enabled:
            return (
                result.system_prompt,
                copy.deepcopy(result.contexts),
                result.current_prompt,
            )
        encoded_system = self.cipher.encode_content(result.system_prompt) if result.system_prompt else ""
        system_prompt = (
            f"{self.cipher.protocol_prompt}\n\n{encoded_system}".strip()
        )
        contexts = []
        for message in result.contexts:
            item = copy.deepcopy(message)
            item["content"] = self.cipher.encode_content(
                self._message_text(message)
            )
            contexts.append(item)
        prompt = (
            self.cipher.encode_content(result.current_prompt)
            if result.current_prompt else ""
        )
        return system_prompt, contexts, prompt

    def cipher_metadata(self, *, provider_encoded: bool) -> dict[str, Any]:
        if not self.cipher_enabled:
            return {
                "enabled": self.cipher_configured,
                "method": self.cipher.method,
                "provider_request_encoded": False,
                "stored_messages": "plaintext",
                "suppressed_by_url_request": (
                    self.cipher_configured and self.url_request_enabled
                ),
            }
        return self.cipher.metadata(provider_encoded=provider_encoded)

    def url_request_metadata(
        self,
        *,
        provider_externalized: bool,
    ) -> dict[str, Any]:
        return {
            "enabled": self.url_request_enabled,
            "provider_request_externalized": bool(
                provider_externalized and self.url_request_enabled
            ),
            "page_format": "html_with_json_content_negotiation",
            "backend": self.url_requests.backend,
            "hosted_proxy_configured": bool(
                self.url_requests.hosted_proxy_url
            ),
            "fetch_tool_enabled": self.url_requests.fetch_tool_enabled,
            "reply_tool_enabled": self.url_requests.reply_tool_enabled,
            "stored_messages": "plaintext",
            "temporary_storage": (
                "remote_d1_with_local_runtime_copy"
                if self.url_requests.backend == "hosted"
                else "memory_only"
            ),
            "cleanup": "delete_on_completion_with_ttl_fallback",
            "ttl_seconds": self.url_requests.ttl_seconds,
            "cipher_suppressed": (
                self.url_request_enabled and self.cipher_configured
            ),
        }

    def decode_model_response(self, text: str) -> tuple[str, bool, str]:
        raw = str(text or "")
        if not self.cipher_enabled:
            return raw, True, ""
        decoded = self.cipher.decode_response(raw)
        if decoded.ok:
            return decoded.text, True, decoded.error
        failure = (
            f"【密文回复自动解码失败：{decoded.error}】\n"
            f"模型原始输出：\n{raw}"
        )
        return failure, False, decoded.error

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    async def set_pending_generation(self, session_id: str, mode: str, prompt: str) -> None:
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            state["pending_generation"] = {"mode": mode, "prompt": prompt}
            await asyncio.to_thread(self.storage.save_session, session_id, state)

    async def reset_session(self, session_id: str) -> None:
        async with self._session_lock(session_id):
            await asyncio.to_thread(self.storage.reset_session, session_id)
            await asyncio.to_thread(self.storage.close_candidate_groups, session_id)

    async def rollback_history_state(self, session_id: str) -> None:
        """Discard derived state from the removed turn while keeping its archive node recoverable."""
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            removed_turn = int(state.get("turn", 0) or 0)
            current_id = str(state.get("current_story_node_id", "") or "")
            current = await asyncio.to_thread(self.storage.get_story_node, current_id) if current_id else None
            parent_id = str((current or {}).get("parent_id", "") or "")
            parent = await asyncio.to_thread(self.storage.get_story_node, parent_id) if parent_id else None
            if parent and isinstance(parent.get("state_snapshot"), dict):
                state = copy.deepcopy(parent["state_snapshot"])
                state["current_story_node_id"] = parent_id
            else:
                state.pop("history_summary", None)
                state.pop("pending_generation", None)
                state.pop("pending_branch", None)
                state["current_story_node_id"] = parent_id
                state["turn"] = max(0, int(state.get("turn", 0) or 0) - 1)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            await asyncio.to_thread(self.storage.delete_preview, session_id)
            await asyncio.to_thread(self.storage.close_candidate_groups, session_id)
            if removed_turn > 0:
                await asyncio.to_thread(
                    self.storage.delete_auto_memories_for_turn, session_id, removed_turn
                )

    async def prepare_swipe(
        self,
        *,
        session_id: str,
        conversation_id: str,
        base_history: list[dict[str, Any]],
        user_message: dict[str, Any],
    ) -> dict[str, Any]:
        """Create/load the latest candidate group and restore its pre-turn state."""
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            current_node_id = str(state.get("current_story_node_id", "") or "")
            current_node = (
                await asyncio.to_thread(self.storage.get_story_node, current_node_id)
                if current_node_id else None
            )
            if not current_node:
                raise ValueError("当前回复没有分支快照，无法创建候选。请确认已启用分支树归档。")

            group = await asyncio.to_thread(
                self.storage.active_candidate_group, session_id
            )
            if group:
                if str(group.get("conversation_id", "")) != str(conversation_id):
                    await asyncio.to_thread(self.storage.close_candidate_groups, session_id)
                    group = None
                elif str(group.get("selected_node_id", "")) != current_node_id:
                    raise ValueError("当前剧情已离开候选回复所在轮次，不能继续 Swipe。")

            if not group:
                base_state = copy.deepcopy(
                    current_node.get("base_state_snapshot", {})
                    if isinstance(current_node.get("base_state_snapshot"), dict)
                    else {}
                )
                if not base_state:
                    parent_id = str(current_node.get("parent_id", "") or "")
                    parent = (
                        await asyncio.to_thread(self.storage.get_story_node, parent_id)
                        if parent_id else None
                    )
                    base_state = copy.deepcopy(
                        (parent or {}).get("state_snapshot", {})
                        if isinstance((parent or {}).get("state_snapshot"), dict)
                        else {}
                    )
                base_campaign = copy.deepcopy(
                    current_node.get("base_campaign_snapshot", {})
                    if isinstance(current_node.get("base_campaign_snapshot"), dict)
                    else {}
                )
                base_state.pop("_campaign_state", None)
                group = await asyncio.to_thread(
                    self.storage.create_candidate_group,
                    {
                        "session_id": session_id,
                        "conversation_id": conversation_id,
                        "parent_node_id": str(current_node.get("parent_id", "") or ""),
                        "source_node_id": current_node_id,
                        "selected_node_id": current_node_id,
                        "user_message": copy.deepcopy(user_message),
                        "base_history": copy.deepcopy(base_history),
                        "base_state": base_state,
                        "base_campaign_state": base_campaign,
                    },
                )

            nodes = await asyncio.to_thread(
                self.storage.list_candidate_nodes, str(group["id"])
            )
            limit = max(2, int(self.config.get("swipe_candidate_limit", 5) or 5))
            if len(nodes) >= limit:
                raise ValueError(f"本轮已经保留 {len(nodes)} 个候选，达到上限 {limit}。")

            restored_state = copy.deepcopy(group.get("base_state", {}))
            restored_state["current_story_node_id"] = str(
                group.get("parent_node_id", "") or ""
            )
            restored_state["astrbot_conversation_id"] = str(conversation_id or "")
            await asyncio.to_thread(self.storage.save_session, session_id, restored_state)
            await asyncio.to_thread(self.storage.delete_preview, session_id)
            removed_turn = int(state.get("turn", 0) or 0)
            if removed_turn > 0:
                await asyncio.to_thread(
                    self.storage.delete_auto_memories_for_turn,
                    session_id,
                    removed_turn,
                )
            campaign = await asyncio.to_thread(
                self.storage.campaign_for_session, session_id
            )
            if campaign and isinstance(group.get("base_campaign_state"), dict):
                campaign["state_data"] = copy.deepcopy(group["base_campaign_state"])
                await asyncio.to_thread(self.storage.save_campaign, campaign)

            return {
                "group_id": str(group["id"]),
                "parent_node_id": str(group.get("parent_node_id", "") or ""),
                "candidate_index": len(nodes) + 1,
                "base_history": copy.deepcopy(group.get("base_history", [])),
                "user_message": copy.deepcopy(group.get("user_message", {})),
            }

    async def candidate_group(self, session_id: str) -> dict[str, Any] | None:
        group = await asyncio.to_thread(self.storage.active_candidate_group, session_id)
        if not group:
            return None
        group["nodes"] = await asyncio.to_thread(
            self.storage.list_candidate_nodes, str(group["id"])
        )
        group["limit"] = max(
            2, int(self.config.get("swipe_candidate_limit", 5) or 5)
        )
        return group

    async def select_candidate(
        self, session_id: str, candidate_index: int
    ) -> dict[str, Any]:
        async with self._session_lock(session_id):
            group = await asyncio.to_thread(
                self.storage.active_candidate_group, session_id
            )
            if not group:
                raise ValueError("当前最新一轮没有可切换的候选回复。")
            nodes = await asyncio.to_thread(
                self.storage.list_candidate_nodes, str(group["id"])
            )
            target = next(
                (
                    node for node in nodes
                    if int(node.get("candidate_index", 0) or 0) == int(candidate_index)
                ),
                None,
            )
            if not target:
                raise ValueError("找不到这个候选编号。")
            await asyncio.to_thread(
                self.storage.select_candidate_node,
                str(group["id"]),
                str(target["id"]),
            )
            await asyncio.to_thread(
                self.storage.sync_candidate_state_changes,
                str(group["id"]),
                str(target["id"]),
            )
            state = copy.deepcopy(target.get("state_snapshot", {}))
            campaign_state = state.pop("_campaign_state", None)
            state["current_story_node_id"] = str(target["id"])
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            preview = target.get("preview_payload")
            if isinstance(preview, dict):
                await asyncio.to_thread(self.storage.save_preview, session_id, preview)
            campaign = await asyncio.to_thread(
                self.storage.campaign_for_session, session_id
            )
            if campaign and isinstance(campaign_state, dict):
                campaign["state_data"] = copy.deepcopy(campaign_state)
                await asyncio.to_thread(self.storage.save_campaign, campaign)
            return target

    async def set_pending_branch(self, session_id: str, node_id: str, branch_name: str = "") -> bool:
        node = await asyncio.to_thread(self.storage.get_story_node, node_id)
        if not node:
            return False
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            state["pending_branch"] = {"source_node_id": node_id, "branch_name": branch_name}
            await asyncio.to_thread(self.storage.save_session, session_id, state)
        return True

    async def finalize_story_snapshot(
        self,
        snapshot: dict[str, Any],
        assistant_text: str,
        assistant_payload: dict[str, Any] | None = None,
    ) -> str:
        session_id = str(snapshot.get("session_id", "") or "default")
        payload = dict(snapshot)
        payload["assistant_text"] = assistant_text
        payload["assistant_payload"] = assistant_payload or {}
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            campaign = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
            payload["state_snapshot"] = dict(
                copy.deepcopy(state),
                **({"_campaign_state": copy.deepcopy(campaign.get("state_data", {}))} if campaign else {}),
            )
            node_id = await asyncio.to_thread(self.storage.create_story_node, payload)
            candidate_group_id = str(payload.get("candidate_group_id", "") or "")
            if candidate_group_id:
                await asyncio.to_thread(
                    self.storage.select_candidate_node, candidate_group_id, node_id
                )
            state["current_story_node_id"] = node_id
            state.pop("archive_new_root", None)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            if campaign:
                messages = payload.get("request_messages", [])
                user_text = ""
                for message in reversed(messages if isinstance(messages, list) else []):
                    if isinstance(message, dict) and message.get("role") == "user":
                        user_text = self._message_text(message)
                        break
                try:
                    await self._extract_campaign_state_change(
                        campaign=campaign, session_id=session_id,
                        turn=int(state.get("turn", 0) or 0), user_text=user_text,
                        assistant_text=assistant_text, story_node_id=node_id,
                    )
                except Exception as exc:
                    logger.warning("%s 战役状态提取失败，已跳过本轮: %s", PLUGIN_TAG, exc)
            final_campaign = await asyncio.to_thread(
                self.storage.campaign_for_session, session_id
            )
            final_snapshot = dict(
                copy.deepcopy(state),
                **(
                    {"_campaign_state": copy.deepcopy(final_campaign.get("state_data", {}))}
                    if final_campaign else {}
                ),
            )
            await asyncio.to_thread(
                self.storage.update_story_node_state, node_id, final_snapshot
            )
            if candidate_group_id:
                await asyncio.to_thread(
                    self.storage.sync_candidate_state_changes,
                    candidate_group_id,
                    node_id,
                )
        return node_id

    def ensure_defaults(self) -> None:
        if not self.storage.list_documents("preset"):
            preset = {
                "main_prompt": "{{original_system}}",
                "blocks": [
                    {"identifier": "main", "name": "Main Prompt", "priority": 0},
                    {"identifier": "world_before", "name": "World Before Character", "priority": 10},
                    {"identifier": "character", "name": "Character Description", "priority": 15},
                    {"identifier": "personality", "name": "Character Personality", "priority": 20},
                    {"identifier": "scenario", "name": "Scenario", "priority": 25},
                    {"identifier": "persona", "name": "Persona", "priority": 30},
                    {"identifier": "examples", "name": "Example Messages", "priority": 60},
                    {"identifier": "author_note", "name": "Author Note", "priority": 40},
                    {"identifier": "world_after", "name": "World After Character", "priority": 35},
                    {"identifier": "summary", "name": "Summary", "priority": 50},
                    {"identifier": "memory", "name": "Vector Memory", "priority": 70},
                    {"identifier": "post_history", "name": "Post-History Instructions", "priority": 5}
                ],
            }
            document_id = self.storage.put_document("preset", "Default", preset)
            self.storage.bind("global", "*", "preset", document_id)
        if not self.storage.list_documents("quick_reply"):
            document_id = self.storage.put_document(
                "quick_reply", "默认快捷回复", {"items": copy.deepcopy(DEFAULT_QUICK_REPLIES)}
            )
            self.storage.bind("global", "*", "quick_reply", document_id)

    def create_pack_from_campaign(self, campaign_id: str, name: str = "") -> dict[str, Any]:
        campaign = self.storage.get_campaign(campaign_id)
        if not campaign:
            raise ValueError("找不到战役")
        bindings = self.storage.list_bindings(scope_type="campaign", scope_id=campaign_id)
        single: dict[str, str] = {}
        additive: dict[str, list[str]] = {"lorebook": [], "material": [], "quick_reply": []}
        for binding in bindings:
            kind, target_id = str(binding.get("kind", "")), str(binding.get("target_id", ""))
            if kind in additive:
                additive[kind].append(target_id)
            elif kind in {"character", "character_group", "preset", "persona"}:
                single[kind] = target_id
        settings = copy.deepcopy(campaign.get("settings", {}))
        settings.setdefault("state_apply_mode", "tiered")
        payload = {
            "world_id": campaign.get("world_id", ""), "ruleset_id": campaign.get("ruleset_id", ""),
            "campaign_description": campaign.get("description", ""), "rule_prompt": campaign.get("rule_prompt", ""),
            "state_template": str(settings.get("state_template", "custom")),
            "state_schema": copy.deepcopy(campaign.get("state_schema", {})),
            "initial_state": copy.deepcopy(campaign.get("state_data", {})),
            "campaign_settings": settings, "single_bindings": single, "additive_bindings": additive,
            "recommended_config": {
                "history_max_messages": 24, "summary_trigger_messages": 36,
                "memory_extract_interval": 8, "memory_extract_mode": "pending",
            },
        }
        pack_id = self.storage.save_rp_pack({
            "name": name or str(campaign.get("name") or "RP 整合包"),
            "description": str(campaign.get("description", "")), "payload": payload,
        })
        return self.storage.get_rp_pack(pack_id) or {}

    async def start_new_game(
        self, *, pack_id: str, session_id: str, name: str = "",
        template_id: str = "", archive_current: bool = True,
        conversation_mode: str = "new",
    ) -> dict[str, Any]:
        pack = await asyncio.to_thread(self.storage.get_rp_pack, pack_id)
        if not pack:
            raise ValueError("找不到 RP 整合包")
        payload = pack.get("payload", {}) if isinstance(pack.get("payload"), dict) else {}
        current = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
        if current and archive_current:
            current["archived"] = True
            await asyncio.to_thread(self.storage.save_campaign, current)
        template = STATE_TEMPLATES.get(template_id or str(payload.get("state_template", "")))
        state_schema = copy.deepcopy(payload.get("state_schema", {}))
        initial_state = copy.deepcopy(payload.get("initial_state", {}))
        if template_id and template:
            state_schema, initial_state = copy.deepcopy(template["schema"]), copy.deepcopy(template["state"])
        settings = copy.deepcopy(payload.get("campaign_settings", {}))
        settings["rp_pack_id"] = pack_id
        settings["state_template"] = template_id or str(payload.get("state_template", "custom"))
        settings.setdefault("state_tracking_enabled", True)
        settings.setdefault("state_extract_interval", 1)
        settings.setdefault("state_apply_mode", "tiered")
        campaign_id = await asyncio.to_thread(self.storage.save_campaign, {
            "name": name or str(pack.get("name") or "新游戏"),
            "world_id": str(payload.get("world_id", "")), "ruleset_id": str(payload.get("ruleset_id", "")),
            "description": str(payload.get("campaign_description", pack.get("description", ""))),
            "rule_prompt": str(payload.get("rule_prompt", "")),
            "state_schema": state_schema, "state_data": initial_state, "settings": settings,
        })
        for kind, target_id in (payload.get("single_bindings", {}) or {}).items():
            if target_id:
                await asyncio.to_thread(self.storage.bind, "campaign", campaign_id, str(kind), str(target_id), 0)
        for kind, target_ids in (payload.get("additive_bindings", {}) or {}).items():
            for target_id in target_ids if isinstance(target_ids, list) else []:
                await asyncio.to_thread(self.storage.bind, "campaign", campaign_id, str(kind), str(target_id), 0)
        for binding in await asyncio.to_thread(self.storage.list_bindings, scope_type="session", scope_id=session_id):
            if str(binding.get("kind")) in {"character", "character_group", "preset", "persona", "lorebook", "material", "quick_reply"}:
                await asyncio.to_thread(
                    self.storage.unbind, str(binding["scope_type"]), str(binding["scope_id"]), str(binding["kind"]), str(binding["target_id"])
                )
        await asyncio.to_thread(self.storage.bind_campaign_session, campaign_id, session_id)
        await self.reset_session(session_id)
        conversation_id = ""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is not None:
            if conversation_mode == "clear":
                conversation_id = str(await manager.get_curr_conversation_id(session_id) or "")
                if conversation_id:
                    await manager.update_conversation(session_id, conversation_id, [])
            else:
                platform_id = session_id.split(":", 1)[0] if ":" in session_id else None
                conversation_id = str(await manager.new_conversation(session_id, platform_id=platform_id))
        return {
            "campaign": await asyncio.to_thread(self.storage.get_campaign, campaign_id),
            "pack": pack, "conversation_id": conversation_id,
            "archived_campaign_id": str((current or {}).get("id", "")),
        }

    def analyze_worldbook(self, document_id: str) -> dict[str, Any]:
        document = self.storage.get_document(document_id)
        if not document or document.get("kind") != "lorebook":
            raise ValueError("找不到世界书")
        entries = normalize_entries(document.get("data", {}), kind="lorebook")
        issues: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        broad = {"任务", "物资", "事件", "地点", "区域", "系统", "状态", "角色", "危险", "积分", "据点", "基地"}
        for entry in entries:
            label = entry.comment or entry.uid
            keys = list(entry.keys)
            joined = [key for key in keys if any(mark in key for mark in ("、", "，", ";", "；"))]
            if joined:
                issues.append({"level": "error", "entry": label, "code": "joined_keywords", "message": "关键词被中文标点连成一个词", "values": joined})
            if not entry.constant and not keys and not entry.vectorized:
                issues.append({"level": "error", "entry": label, "code": "unreachable", "message": "非常驻、无关键词且未向量化，条目不会命中"})
            if entry.constant and len(entry.content) > 1200:
                issues.append({"level": "warning", "entry": label, "code": "large_constant", "message": f"常驻内容较大（{len(entry.content)}字），建议按需触发"})
            if not entry.constant and len(entry.content) > 800 and not entry.vectorized:
                issues.append({"level": "info", "entry": label, "code": "vector_candidate", "message": "长条目建议开启向量化"})
            for key in keys:
                normalized = key.strip().lower()
                if normalized in broad:
                    issues.append({"level": "warning", "entry": label, "code": "broad_keyword", "message": f"关键词“{key}”可能频繁误触发"})
                if normalized in seen and seen[normalized] != label:
                    issues.append({"level": "warning", "entry": label, "code": "duplicate_keyword", "message": f"关键词“{key}”也用于“{seen[normalized]}”"})
                seen[normalized] = label
        return {"document_id": document_id, "name": document.get("name", ""), "entry_count": len(entries), "issues": issues}

    def scopes(self, event: Any, req: Any) -> list[tuple[str, str]]:
        result = [("global", "*")]
        get_extra = getattr(event, "get_extra", lambda _key: None)
        for scope_type, value in (
            ("session", getattr(event, "unified_msg_origin", "")),
            ("conversation", str(get_extra("_kt_astrbot_conversation_id") or "")),
            ("user", str(event.get_sender_id() or "")),
            ("group", str(event.get_group_id() or "")),
            ("persona", str(getattr(getattr(req, "conversation", None), "persona_id", "") or "")),
        ):
            if value:
                result.append((scope_type, value))
        session_id = str(getattr(event, "unified_msg_origin", "") or getattr(req, "session_id", "") or "")
        campaign = self.storage.campaign_for_session(session_id) if session_id else None
        if campaign:
            if campaign.get("world_id"):
                result.append(("world", str(campaign["world_id"])))
            if campaign.get("ruleset_id"):
                result.append(("ruleset", str(campaign["ruleset_id"])))
            result.append(("campaign", str(campaign["id"])))
        return result

    def _bound_one(self, kind: str, scopes: list[tuple[str, str]]) -> dict[str, Any] | None:
        by_type = {scope_type: (scope_type, scope_id) for scope_type, scope_id in scopes}
        for scope_type in ("session", "campaign", "ruleset", "world", "persona", "user", "group", "global"):
            scope = by_type.get(scope_type)
            if not scope:
                continue
            documents = self.storage.resolve_bindings(kind, [scope])
            if documents:
                return documents[0]
        return None

    def effective_bindings(self, scopes: list[tuple[str, str]]) -> dict[str, Any]:
        single = {}
        for kind in ("preset", "character", "character_group", "persona"):
            document = self._bound_one(kind, scopes)
            single[kind] = document
        additive = {
            kind: self.storage.resolve_bindings(kind, scopes)
            for kind in ("lorebook", "material", "quick_reply")
        }
        return {"scopes": scopes, "single": single, "additive": additive}

    def quick_replies(self, scopes: list[tuple[str, str]]) -> list[dict[str, Any]]:
        replies: list[dict[str, Any]] = []
        for document in self.storage.resolve_bindings("quick_reply", scopes):
            data = document.get("data", {}) if isinstance(document.get("data"), dict) else {}
            items = data.get("items", []) if isinstance(data.get("items", []), list) else []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not item.get("enabled", True):
                    continue
                replies.append({
                    "document_id": document.get("id", ""),
                    "document_name": document.get("name", ""),
                    "index": len(replies) + 1,
                    "item_index": index + 1,
                    "id": str(item.get("id", "")),
                    "label": str(item.get("label", "")),
                    "alias": str(item.get("alias", "")),
                    "content": str(item.get("content", "")),
                    "mode": str(item.get("mode", "normal") or "normal"),
                    "append_input": bool(item.get("append_input", True)),
                    "order": int(item.get("order", index * 10) or 0),
                })
        replies.sort(key=lambda item: (str(item.get("document_name", "")), int(item.get("order", 0)), str(item.get("label", ""))))
        for index, item in enumerate(replies, 1):
            item["index"] = index
        return replies

    def find_quick_reply(self, scopes: list[tuple[str, str]], query: str) -> dict[str, Any] | None:
        needle = str(query or "").strip().lower()
        if not needle:
            return None
        replies = self.quick_replies(scopes)
        if needle.isdigit():
            number = int(needle)
            return next((item for item in replies if int(item.get("index", 0)) == number), None)
        exact = next((
            item for item in replies
            if needle in {str(item.get("alias", "")).lower(), str(item.get("label", "")).lower(), str(item.get("id", "")).lower()}
        ), None)
        if exact:
            return exact
        return next((item for item in replies if needle in str(item.get("label", "")).lower()), None)

    def quick_reply_prompt(self, item: dict[str, Any], rest: str = "") -> tuple[str, str, str]:
        content = str(item.get("content", "")).strip()
        extra = str(rest or "").strip()
        if extra and bool(item.get("append_input", True)):
            prompt = f"{content}\n\n{extra}".strip()
        else:
            prompt = content or extra
        mode = str(item.get("mode", "normal") or "normal")
        if mode not in {"normal", "continue", "impersonate", "quiet"}:
            mode = "normal"
        quiet_prompt = prompt if mode == "quiet" else ""
        return mode, prompt, quiet_prompt

    @staticmethod
    def _document_ref(document: dict[str, Any] | None) -> dict[str, Any] | None:
        if not document:
            return None
        return {"id": document.get("id", ""), "name": document.get("name", "")}

    def _group_members(self, group_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not group_doc:
            return []
        group = group_doc.get("data", {})
        member_ids = [str(item) for item in group.get("members", []) if str(item).strip()]
        members = [self.storage.get_document(item) for item in member_ids]
        return [item for item in members if item and item.get("kind") == "character"]

    @staticmethod
    def _character_name(document: dict[str, Any] | None) -> str:
        if not document:
            return ""
        data = document.get("data", {})
        card = data.get("data", data) if isinstance(data, dict) else {}
        return str(card.get("name") or document.get("name") or "")

    def _select_character_with_meta(
        self, scopes: list[tuple[str, str]], prompt: str, state: dict[str, Any], *, advance: bool = True
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        group_doc = self._bound_one("character_group", scopes)
        meta: dict[str, Any] = {
            "group": None,
            "character": None,
            "members": [],
            "reason": "none",
            "index": None,
            "next_index": state.get("group_index", 0),
            "forced": False,
        }
        if not group_doc:
            selected = self._bound_one("character", scopes)
            meta["character"] = self._document_ref(selected)
            meta["reason"] = "single" if selected else "none"
            return selected, meta

        group = group_doc.get("data", {})
        selection = str(group.get("selection", "round_robin") or "round_robin")
        members = self._group_members(group_doc)
        meta["group"] = {"id": group_doc.get("id", ""), "name": group_doc.get("name", ""), "selection": selection}
        meta["members"] = [
            {"id": item.get("id", ""), "name": item.get("name", ""), "card_name": self._character_name(item)}
            for item in members
        ]
        if not members:
            selected = self._bound_one("character", scopes)
            meta["character"] = self._document_ref(selected)
            meta["reason"] = "fallback_single" if selected else "none"
            return selected, meta

        lowered = prompt.lower()
        selected = next((item for item in members if self._character_name(item).lower() in lowered), None)
        reason = "mentioned" if selected is not None else ""
        forced_id = str(state.get("forced_character_id", "") or "")
        if selected is None and forced_id:
            selected = next((item for item in members if item.get("id") == forced_id), None)
            reason = "forced" if selected is not None else ""

        if selected is None:
            index = int(state.get("group_index", 0) or 0) % len(members)
            selected = members[index]
            reason = "round_robin" if selection == "round_robin" else "manual"
            if selection == "round_robin" and advance:
                state["group_index"] = (index + 1) % len(members)
        else:
            index = members.index(selected)

        meta["character"] = {"id": selected.get("id", ""), "name": selected.get("name", ""), "card_name": self._character_name(selected)}
        meta["reason"] = reason
        meta["index"] = index
        meta["next_index"] = state.get("group_index", 0)
        meta["forced"] = reason == "forced"
        return selected, meta

    def _select_character(
        self, scopes: list[tuple[str, str]], prompt: str, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        selected, _ = self._select_character_with_meta(scopes, prompt, state)
        return selected

    def _character_group_status(self, scopes: list[tuple[str, str]], state: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        group_doc = self._bound_one("character_group", scopes)
        members = self._group_members(group_doc)
        forced_id = str(state.get("forced_character_id", "") or "")
        return group_doc, members, forced_id

    def character_status(self, scopes: list[tuple[str, str]], session_id: str) -> str:
        state = self.storage.get_session(session_id)
        group_doc, members, forced_id = self._character_group_status(scopes, state)
        if not group_doc:
            selected = self._bound_one("character", scopes)
            name = self._character_name(selected) if selected else "无"
            return f"当前作用域没有绑定角色组。普通角色卡：{name}"
        group = group_doc.get("data", {})
        index = int(state.get("group_index", 0) or 0) % len(members) if members else 0
        forced = next((item for item in members if item.get("id") == forced_id), None)
        current = forced or (members[index] if members else None)
        lines = [
            f"角色组：{group_doc.get('name', '')}",
            f"策略：{group.get('selection', 'round_robin')}",
            f"当前角色：{self._character_name(current) if current else '无'}",
            f"手动锁定：{self._character_name(forced) if forced else '无'}",
            "成员：",
        ]
        lines.extend(f"{i + 1}. {self._character_name(item)}" for i, item in enumerate(members))
        return "\n".join(lines)

    def character_next(self, scopes: list[tuple[str, str]], session_id: str) -> str:
        state = self.storage.get_session(session_id)
        group_doc, members, _ = self._character_group_status(scopes, state)
        if not group_doc or not members:
            return "当前作用域没有可切换的角色组。"
        forced_id = str(state.get("forced_character_id", "") or "")
        forced_index = next((i for i, item in enumerate(members) if item.get("id") == forced_id), None)
        current_index = forced_index if forced_index is not None else int(state.get("group_index", 0) or 0) % len(members)
        index = (current_index + 1) % len(members)
        selected = members[index]
        state["group_index"] = index
        state.pop("forced_character_id", None)
        self.storage.save_session(session_id, state)
        return f"已切换到：{self._character_name(selected)}"

    def character_use(self, scopes: list[tuple[str, str]], session_id: str, name: str) -> str:
        state = self.storage.get_session(session_id)
        group_doc, members, _ = self._character_group_status(scopes, state)
        if not group_doc or not members:
            return "当前作用域没有可切换的角色组。"
        needle = name.strip().lower()
        if not needle:
            return "请提供角色名，例如：/tavern character use Alice"
        selected = next((item for item in members if needle in self._character_name(item).lower()), None)
        if not selected:
            return "角色组中没有找到该角色。"
        state["forced_character_id"] = selected.get("id", "")
        state["group_index"] = members.index(selected)
        self.storage.save_session(session_id, state)
        return f"已锁定角色：{self._character_name(selected)}"

    def _cache_get(self, key: str) -> list[float] | None:
        value = self._embedding_cache.get(key)
        if value is not None:
            self._embedding_cache.move_to_end(key)
        return value

    def _cache_put(self, key: str, value: list[float]) -> None:
        self._embedding_cache[key] = value
        self._embedding_cache.move_to_end(key)
        while len(self._embedding_cache) > self._embedding_cache_limit:
            self._embedding_cache.popitem(last=False)

    def _embedding_provider(self):
        provider_id = str(self.config.get("embedding_provider_id", ""))
        providers = list(self.context.get_all_embedding_providers())
        return next((item for item in providers if str(item.provider_config.get("id", "")) == provider_id), None)

    async def _embedding(self, text: str) -> list[float]:
        provider = self._embedding_provider()
        if provider is None:
            return []
        return list(await provider.get_embedding(text))

    def _embedding_model_id(self) -> str:
        return str(self.config.get("embedding_provider_id", ""))

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0

    async def _vector_matcher(self, text: str, entries: list[LoreEntry]) -> dict[str, float]:
        if not self.config.get("vector_enabled", False):
            return {}
        try:
            provider = self._embedding_provider()
            if provider is None:
                return {}
            embedding_model = str(self.config.get("embedding_provider_id", ""))
            query = await provider.get_embedding(text)
            result: dict[str, float] = {}
            for entry in entries:
                if not entry.vectorized or entry.disabled or not entry.content:
                    continue
                content_hash = hashlib.sha1(entry.content.encode("utf-8")).hexdigest()
                cache_key = f"{embedding_model}:{content_hash}"
                vector = self._cache_get(cache_key)
                if vector is None:
                    vector = await asyncio.to_thread(
                        self.storage.get_entry_embedding_by_hash, content_hash, embedding_model
                    )
                    if vector:
                        self._cache_put(cache_key, vector)
                if not vector:
                    vector = list(await provider.get_embedding(entry.content))
                    self._cache_put(cache_key, vector)
                    await asyncio.to_thread(
                        self.storage.update_entry_embedding,
                        content_hash,
                        vector,
                        embedding_model,
                    )
                norm = math.sqrt(sum(x * x for x in query)) * math.sqrt(sum(x * x for x in vector))
                score = sum(a * b for a, b in zip(query, vector)) / norm if norm else 0.0
                threshold = float(entry.raw.get("vector_threshold", 0.35))
                if score >= threshold:
                    result[entry.uid] = score
            return result
        except Exception as exc:
            logger.warning("%s 向量匹配失败，已降级跳过向量条目: %s", PLUGIN_TAG, exc)
            return {}

    async def _hybrid_matcher(self, text: str, entries: list[LoreEntry]) -> dict[str, float]:
        mode = str(self.config.get("retrieval_mode", "hybrid") or "hybrid")

        if mode in {"vector", "hybrid"} and not self.config.get("vector_enabled", False):
            if mode == "vector":
                return {}
            mode = "keyword"

        keyword_weight = float(self.config.get("keyword_weight", 0.35))
        vector_weight = float(self.config.get("vector_weight", 0.65))
        retrieval_top_k = int(self.config.get("retrieval_top_k", 8))
        retrieval_candidate_k = int(self.config.get("retrieval_candidate_k", 60))

        allowed_by_hash: dict[str, LoreEntry] = {}
        for entry in entries:
            if entry.content:
                content_hash = hashlib.sha1(entry.content.encode("utf-8")).hexdigest()
                allowed_by_hash[content_hash] = entry

        keyword_scores: dict[str, float] = {}
        if mode in {"keyword", "hybrid"}:
            try:
                fts_results = await asyncio.to_thread(
                    self.storage.search_entries_fts, text, limit=retrieval_candidate_k
                )
                for item in fts_results:
                    content_hash = item.get("content_hash", "")
                    if content_hash in allowed_by_hash:
                        entry = allowed_by_hash[content_hash]
                        keyword_scores[entry.uid] = float(item.get("score", 0.5))
            except Exception as exc:
                logger.warning("%s 关键词检索失败，已降级: %s", PLUGIN_TAG, exc)

        vector_scores: dict[str, float] = {}
        if mode in {"vector", "hybrid"}:
            vector_scores = await self._vector_matcher(text, entries)

        if mode == "keyword":
            combined = keyword_scores
        elif mode == "vector":
            combined = vector_scores
        else:
            all_uids = set(keyword_scores) | set(vector_scores)
            combined = {}
            for uid in all_uids:
                kw_score = keyword_scores.get(uid, 0.0)
                vec_score = vector_scores.get(uid, 0.0)
                combined[uid] = keyword_weight * kw_score + vector_weight * vec_score

        sorted_items = sorted(combined.items(), key=lambda x: x[1], reverse=True)

        category_dedup_limit = int(self.config.get("retrieval_category_dedup_limit", 2) or 0)
        if category_dedup_limit > 0:
            entries_by_uid = {entry.uid: entry for entry in entries}
            category_counts: dict[str, int] = {}
            filtered_items: list[tuple[str, float]] = []
            for uid, score in sorted_items:
                entry = entries_by_uid.get(uid)
                if not entry:
                    filtered_items.append((uid, score))
                    continue
                ext = entry.raw.get("extensions") if isinstance(entry.raw.get("extensions"), dict) else {}
                category = str(entry.raw.get("category", ext.get("category", entry.group or "")) or "")
                if not category:
                    filtered_items.append((uid, score))
                    continue
                if category_counts.get(category, 0) < category_dedup_limit:
                    category_counts[category] = category_counts.get(category, 0) + 1
                    filtered_items.append((uid, score))
            sorted_items = filtered_items

        if len(sorted_items) > retrieval_top_k:
            sorted_items = sorted_items[:retrieval_top_k]

        return dict(sorted_items)

    async def _retrieve_memories(
        self,
        *,
        scopes: list[tuple[str, str]],
        text: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self.config.get("memory_enabled", False):
            return "", []
        try:
            query = await self._embedding(text)
            if not query:
                return "", []
            candidates: list[dict[str, Any]] = []
            has_conversation_scope = any(scope_type == "conversation" for scope_type, _ in scopes)
            for scope_type, scope_id in scopes:
                scoped = await asyncio.to_thread(
                    self.storage.list_memories,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    enabled=True,
                    status="active",
                    include_expired=False,
                    limit=500,
                )
                if scope_type == "session" and has_conversation_scope:
                    scoped = [item for item in scoped if item.get("source_type") != "auto_extract"]
                candidates.extend(scoped)
            seen: set[str] = set()
            scored: list[tuple[float, dict[str, Any]]] = []
            for item in candidates:
                memory_id = str(item.get("id", ""))
                if not memory_id or memory_id in seen:
                    continue
                seen.add(memory_id)
                score = self._cosine(query, item.get("embedding", []))
                if score > 0:
                    importance = max(0.0, float(item.get("importance", 1.0) or 1.0))
                    scored.append((score * importance, item))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            top_k = max(1, int(self.config.get("memory_top_k", 5) or 5))
            matches = [dict(item, score=score) for score, item in scored[:top_k]]
            lines = [
                f"- [{item.get('category') or 'memory'}] {item.get('content')}"
                for item in matches
            ]
            return "\n".join(lines), matches
        except Exception as exc:
            logger.warning("%s 长期记忆检索失败，已降级跳过: %s", PLUGIN_TAG, exc)
            return "", []

    def _memory_provider(self, session_id: str):
        configured_id = str(self.config.get("memory_provider_id", "") or "").strip()
        if configured_id:
            provider = self.context.get_provider_by_id(configured_id)
            if provider is None:
                raise RuntimeError(f"找不到记忆 Provider: {configured_id}")
            return provider, configured_id
        configured_id = str(self.config.get("summary_provider_id", "") or "").strip()
        if configured_id:
            provider = self.context.get_provider_by_id(configured_id)
            if provider is None:
                raise RuntimeError(f"找不到摘要 Provider: {configured_id}")
            return provider, configured_id
        provider = self.context.get_using_provider(session_id)
        if provider is None:
            raise RuntimeError("当前会话没有可用的记忆 Provider")
        return provider, str(getattr(provider, "provider_config", {}).get("id", "current"))

    async def _text_chat_with_fallback(
        self, *, session_id: str, provider: Any, **kwargs: Any,
    ) -> tuple[Any, str]:
        """Call an auxiliary model using AstrBot's ordered fallback list."""
        candidates: list[tuple[Any, str]] = []
        seen: set[str] = set()

        def build_call_variants() -> list[dict[str, Any]]:
            variants: list[dict[str, Any]] = []
            passthrough = {
                key: value for key, value in kwargs.items()
                if key not in {"messages", "prompt", "contexts", "system_prompt"}
            }

            def add_variant(payload: dict[str, Any]) -> None:
                cleaned: dict[str, Any] = {}
                for key, value in payload.items():
                    if key in {"prompt", "system_prompt"} and not str(value or ""):
                        continue
                    if key == "contexts" and not value:
                        continue
                    cleaned[key] = value
                if cleaned and all(cleaned != item for item in variants):
                    variants.append(cleaned)

            def split_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str]:
                system_prompt = "\n\n".join(
                    self._message_text(item) for item in messages if item.get("role") == "system"
                ).strip()
                chat_messages = [item for item in messages if item.get("role") != "system"]
                prompt = ""
                contexts = list(chat_messages)
                if contexts and contexts[-1].get("role") == "user":
                    prompt = self._message_text(contexts[-1])
                    contexts = contexts[:-1]
                return system_prompt, contexts, prompt

            if "messages" in kwargs:
                normalized = [
                    item for item in (
                        self._normalize_message(item) for item in list(kwargs.get("messages") or [])
                    )
                    if item is not None
                ]
                if normalized:
                    system_prompt, contexts, prompt = split_messages(normalized)
                    add_variant({**passthrough, "contexts": contexts, "prompt": prompt, "system_prompt": system_prompt})
                    add_variant({**passthrough, "contexts": normalized})
                    add_variant({**passthrough, "prompt": self._messages_as_transcript(normalized)})
            else:
                contexts = self._normalize_messages(list(kwargs.get("contexts") or []))
                prompt = str(kwargs.get("prompt", "") or "")
                system_prompt = str(kwargs.get("system_prompt", "") or "").strip()
                add_variant({**passthrough, "contexts": contexts, "prompt": prompt, "system_prompt": system_prompt})
                if contexts or system_prompt:
                    normalized = (
                        ([{"role": "system", "content": system_prompt}] if system_prompt else [])
                        + contexts
                        + ([{"role": "user", "content": prompt}] if prompt else [])
                    )
                    add_variant({**passthrough, "contexts": normalized})
                    if prompt and contexts:
                        add_variant({**passthrough, "prompt": self._messages_as_transcript(normalized)})
            return variants

        def add_candidate(candidate: Any, candidate_id: str = "") -> None:
            if candidate is None:
                return
            resolved_id = candidate_id or str(
                getattr(candidate, "provider_config", {}).get("id", "")
            )
            identity = resolved_id or f"object:{id(candidate)}"
            if identity in seen:
                return
            seen.add(identity)
            candidates.append((candidate, resolved_id or "current"))

        add_candidate(provider)
        try:
            astrbot_config = self.context.get_config(umo=session_id)
            provider_settings = astrbot_config.get("provider_settings", {})
            fallback_ids = provider_settings.get("fallback_chat_models", [])
            if isinstance(fallback_ids, list):
                for fallback_id in fallback_ids:
                    if isinstance(fallback_id, str) and fallback_id:
                        add_candidate(
                            self.context.get_provider_by_id(fallback_id), fallback_id
                        )
        except Exception as exc:
            logger.debug("%s 读取 AstrBot 备用 Provider 列表失败: %s", PLUGIN_TAG, exc)

        call_variants = build_call_variants()
        last_error: Exception | None = None
        previous_id = candidates[0][1] if candidates else "current"
        for index, (candidate, candidate_id) in enumerate(candidates):
            if index:
                logger.warning(
                    "%s 辅助模型从 %s 切换到备用 Provider: %s",
                    PLUGIN_TAG, previous_id, candidate_id,
                )
            candidate_error: Exception | None = None
            for call_kwargs in call_variants:
                try:
                    response = await candidate.text_chat(**call_kwargs)
                    if str(getattr(response, "role", "")) == "err":
                        raise RuntimeError(
                            str(getattr(response, "completion_text", "") or "Provider 返回错误响应")
                        )
                    return response, candidate_id
                except Exception as exc:
                    candidate_error = exc
                    last_error = exc
                    logger.debug(
                        "%s 辅助模型 Provider %s 调用形式 %s 失败: %s",
                        PLUGIN_TAG, candidate_id,
                        "contexts" if "contexts" in call_kwargs else "prompt", exc,
                    )
            if candidate_error:
                logger.warning(
                    "%s 辅助模型 Provider %s 调用失败: %s",
                    PLUGIN_TAG, candidate_id, candidate_error,
                )
            previous_id = candidate_id
        if last_error:
            raise last_error
        raise RuntimeError("没有可用的辅助模型 Provider")

    @staticmethod
    def _messages_as_transcript(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "unknown") or "unknown").upper()
            content = TavernService._message_text(message).strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n\n".join(lines)

    async def _chat_completion_with_fallback(
        self, *, session_id: str, messages: list[dict[str, Any]], provider: Any | None = None,
    ) -> tuple[Any, str]:
        provider = provider or self.context.get_using_provider(session_id)
        if provider is None:
            raise RuntimeError("当前会话没有可用的聊天 Provider")
        normalized = [item for item in (self._normalize_message(item) for item in messages) if item is not None]
        if not normalized:
            raise ValueError("聊天请求 messages[] 为空。")
        system_prompt = "\n\n".join(
            self._message_text(item) for item in normalized if item.get("role") == "system"
        ).strip()
        chat_messages = [item for item in normalized if item.get("role") != "system"]
        current_prompt = ""
        contexts = list(chat_messages)
        if contexts and contexts[-1].get("role") == "user":
            current_prompt = self._message_text(contexts[-1])
            contexts = contexts[:-1]
        transcript = self._messages_as_transcript(normalized)
        if not current_prompt:
            current_prompt = transcript
        return await self._text_chat_with_fallback(
            session_id=session_id,
            provider=provider,
            prompt=current_prompt,
            contexts=contexts,
            system_prompt=system_prompt,
        )

    def _url_provider_candidates(
        self,
        *,
        session_id: str,
        provider: Any,
    ) -> list[tuple[Any, str]]:
        candidates: list[tuple[Any, str]] = []
        seen: set[str] = set()

        def add(candidate: Any, candidate_id: str = "") -> None:
            if candidate is None:
                return
            resolved_id = candidate_id or str(
                getattr(candidate, "provider_config", {}).get("id", "")
            )
            identity = resolved_id or f"object:{id(candidate)}"
            if identity in seen:
                return
            seen.add(identity)
            candidates.append((candidate, resolved_id or "current"))

        add(provider)
        try:
            astrbot_config = self.context.get_config(umo=session_id)
            provider_settings = astrbot_config.get("provider_settings", {})
            fallback_ids = provider_settings.get("fallback_chat_models", [])
            if isinstance(fallback_ids, list):
                for fallback_id in fallback_ids:
                    if isinstance(fallback_id, str) and fallback_id:
                        add(
                            self.context.get_provider_by_id(fallback_id),
                            fallback_id,
                        )
        except Exception as exc:
            logger.debug(
                "%s 读取 AstrBot 备用 Provider 列表失败: %s",
                PLUGIN_TAG,
                exc,
            )
        return candidates

    @staticmethod
    def _validate_url_tool_call(response: Any) -> tuple[str, dict[str, Any], str]:
        names = list(getattr(response, "tools_call_name", []) or [])
        args = list(getattr(response, "tools_call_args", []) or [])
        call_ids = list(getattr(response, "tools_call_ids", []) or [])
        if len(names) != 1 or len(args) != 1 or len(call_ids) != 1:
            raise URLRequestProtocolError(
                "网址请求模式只允许一次 fetch_request_url 工具调用。"
            )
        if names[0] not in {FETCH_TOOL_NAME, SUBMIT_REPLY_TOOL_NAME}:
            raise URLRequestProtocolError(
                f"网址请求模式不允许调用工具 {names[0]!r}。"
            )
        if not isinstance(args[0], dict):
            raise URLRequestProtocolError("fetch_request_url 工具参数格式无效。")
        return names[0], args[0], str(call_ids[0] or "")

    async def _url_chat_completion_with_fallback_legacy(
        self,
        *,
        session_id: str,
        provider: Any,
        handle: URLRequestHandle,
    ) -> tuple[Any, str]:
        candidates = self._url_provider_candidates(
            session_id=session_id,
            provider=provider,
        )
        if not candidates:
            raise RuntimeError("当前会话没有可用的聊天 Provider")

        tool_set = (
            ToolSet(tools=[build_fetch_tool()])
            if self.url_requests.fetch_tool_enabled
            else None
        )
        protocol = self.url_requests.protocol_prompt
        url_prompt = request_url_user_prompt(handle.url)
        last_error: Exception | None = None
        previous_id = candidates[0][1]

        for index, (candidate, candidate_id) in enumerate(candidates):
            if index:
                logger.warning(
                    "%s 网址请求从 %s 切换到备用 Provider: %s",
                    PLUGIN_TAG,
                    previous_id,
                    candidate_id,
                )
            consumer_id = f"web:{index}:{candidate_id}"
            try:
                first = await candidate.text_chat(
                    prompt=url_prompt,
                    contexts=[],
                    system_prompt=protocol,
                    func_tool=tool_set,
                )
                if str(getattr(first, "role", "")) == "err":
                    raise RuntimeError(
                        str(
                            getattr(first, "completion_text", "")
                            or "Provider 返回错误响应"
                        )
                    )
                if not list(getattr(first, "tools_call_name", []) or []):
                    return first, candidate_id
                if not self.url_requests.fetch_tool_enabled:
                    raise URLRequestProtocolError(
                        "当前网址请求未启用读取工具，但模型仍请求了工具调用。"
                    )

                _, arguments, call_id = self._validate_url_tool_call(first)
                requested_url = str(arguments.get("url", "") or "")
                try:
                    tool_content = self.url_requests.tool_content(
                        handle,
                        requested_url,
                        consumer_id=consumer_id,
                    )
                except ValueError as exc:
                    raise URLRequestProtocolError(str(exc)) from exc
                second_contexts = [
                    {"role": "user", "content": url_prompt},
                    {
                        "role": "assistant",
                        "content": str(getattr(first, "completion_text", "") or ""),
                        "tool_calls": first.to_openai_tool_calls(),
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": FETCH_TOOL_NAME,
                        "content": tool_content,
                    },
                ]
                second = await candidate.text_chat(
                    prompt="Produce the final answer to the hosted request now.",
                    contexts=second_contexts,
                    system_prompt=protocol,
                    func_tool=tool_set,
                )
                if str(getattr(second, "role", "")) == "err":
                    raise RuntimeError(
                        str(
                            getattr(second, "completion_text", "")
                            or "Provider 返回错误响应"
                        )
                    )
                if list(getattr(second, "tools_call_name", []) or []):
                    raise URLRequestProtocolError(
                        "fetch_request_url 已调用一次，模型不得重复调用工具。"
                    )
                return second, candidate_id
            except URLRequestProtocolError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "%s 网址请求 Provider %s 调用失败: %s",
                    PLUGIN_TAG,
                    candidate_id,
                    exc,
                )
            previous_id = candidate_id

        if last_error:
            raise last_error
        raise RuntimeError("没有可用的聊天 Provider")

    async def _url_chat_completion_with_fallback(
        self,
        *,
        session_id: str,
        provider: Any,
        handle: URLRequestHandle,
    ) -> tuple[Any, str]:
        candidates = self._url_provider_candidates(
            session_id=session_id,
            provider=provider,
        )
        if not candidates:
            raise RuntimeError("当前会话没有可用的聊天 Provider。")

        tools = []
        if self.url_requests.fetch_tool_enabled:
            tools.append(build_fetch_tool())
        if self.url_requests.reply_tool_enabled:
            tools.append(build_submit_reply_tool())
        tool_set = ToolSet(tools=tools) if tools else None
        protocol = self.url_requests.protocol_prompt
        url_prompt = request_url_user_prompt(handle.url)
        last_error: Exception | None = None
        previous_id = candidates[0][1]

        for index, (candidate, candidate_id) in enumerate(candidates):
            if index:
                logger.warning(
                    "%s 网址请求从 %s 切换到备用 Provider: %s",
                    PLUGIN_TAG,
                    previous_id,
                    candidate_id,
                )
            consumer_id = f"web:{index}:{candidate_id}"
            try:
                contexts: list[dict[str, Any]] = []
                prompt = url_prompt
                fetch_used = False
                for _step in range(3):
                    response = await candidate.text_chat(
                        prompt=prompt,
                        contexts=copy.deepcopy(contexts),
                        system_prompt=protocol,
                        func_tool=tool_set,
                    )
                    if str(getattr(response, "role", "")) == "err":
                        raise RuntimeError(
                            str(
                                getattr(response, "completion_text", "")
                                or "Provider returned an error response."
                            )
                        )
                    if not list(getattr(response, "tools_call_name", []) or []):
                        return response, candidate_id

                    tool_name, arguments, call_id = self._validate_url_tool_call(
                        response
                    )
                    if tool_name == SUBMIT_REPLY_TOOL_NAME:
                        if not self.url_requests.reply_tool_enabled:
                            raise URLRequestProtocolError(
                                "submit_reply is disabled for URL request mode."
                            )
                        try:
                            reply = await self.url_requests.submit_reply(
                                handle,
                                str(arguments.get("text", "") or ""),
                                consumer_id=consumer_id,
                            )
                        except ValueError as exc:
                            raise URLRequestProtocolError(str(exc)) from exc
                        response.role = "assistant"
                        response.completion_text = reply
                        response.tools_call_name = []
                        response.tools_call_args = []
                        response.tools_call_ids = []
                        return response, candidate_id

                    if not self.url_requests.fetch_tool_enabled or fetch_used:
                        raise URLRequestProtocolError(
                            "fetch_request_url may be called at most once."
                        )
                    try:
                        tool_content = self.url_requests.tool_content(
                            handle,
                            str(arguments.get("url", "") or ""),
                            consumer_id=consumer_id,
                        )
                    except ValueError as exc:
                        raise URLRequestProtocolError(str(exc)) from exc
                    fetch_used = True
                    contexts.extend([
                        {"role": "user", "content": prompt},
                        {
                            "role": "assistant",
                            "content": str(
                                getattr(response, "completion_text", "") or ""
                            ),
                            "tool_calls": response.to_openai_tool_calls(),
                        },
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": FETCH_TOOL_NAME,
                            "content": tool_content,
                        },
                    ])
                    prompt = (
                        "Continue the hosted request. Submit the completed final "
                        "reply through submit_reply when ready."
                    )
                raise URLRequestProtocolError(
                    "URL request mode exceeded the allowed tool rounds."
                )
            except URLRequestProtocolError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "%s 网址请求 Provider %s 调用失败: %s",
                    PLUGIN_TAG,
                    candidate_id,
                    exc,
                )
            previous_id = candidate_id

        if last_error:
            raise last_error
        raise RuntimeError("没有可用的聊天 Provider。")

    @staticmethod
    def _parse_memory_items(text: str) -> list[dict[str, str]]:
        raw = text.strip()
        start, end = raw.find("["), raw.rfind("]")
        if start >= 0 and end >= start:
            raw = raw[start:end + 1]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        allowed = {"preference", "relationship", "plot", "status"}
        result: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            category = str(item.get("category", "status")).strip().lower()
            result.append({"category": category if category in allowed else "status", "content": content})
        return result

    async def _extract_memories(
        self,
        *,
        session_id: str,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[str]:
        if not self.config.get("memory_enabled", False):
            return []
        interval = max(1, int(self.config.get("memory_extract_interval", 12) or 12))
        turn = int(state.get("turn", 0) or 0)
        last_turn = int(state.get("last_memory_turn", 0) or 0)
        if turn <= 0 or turn - last_turn < interval:
            return []
        try:
            provider, provider_id = self._memory_provider(session_id)
            recent = messages[-max(2, interval):]
            transcript = "\n".join(
                f"{str(message.get('role', 'unknown')).upper()}: {self._message_text(message)}"
                for message in recent
            )
            template = str(self.config.get("memory_prompt", DEFAULT_MEMORY_PROMPT) or DEFAULT_MEMORY_PROMPT)
            prompt = template.replace("{history}", transcript)
            response, provider_id = await self._text_chat_with_fallback(
                session_id=session_id,
                provider=provider,
                prompt=prompt,
                max_tokens=max(128, int(self.config.get("memory_max_tokens", 512) or 512)),
                temperature=0.1,
            )
            items = self._parse_memory_items(str(getattr(response, "completion_text", "") or ""))
            written: list[str] = []
            extract_mode = str(self.config.get("memory_extract_mode", "auto") or "auto")
            status = "pending" if extract_mode == "pending" else "active"
            campaign = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
            conversation_id = str(state.get("astrbot_conversation_id", "") or "")
            memory_scope_type = "campaign" if campaign else "conversation" if conversation_id else "session"
            memory_scope_id = str(campaign["id"]) if campaign else conversation_id or session_id
            for item in items:
                embedding = await self._embedding(item["content"])
                if not embedding:
                    continue
                memory_id = await asyncio.to_thread(
                    self.storage.put_memory,
                    scope_type=memory_scope_type,
                    scope_id=memory_scope_id,
                    category=item["category"],
                    content=item["content"],
                    embedding=embedding,
                    embedding_model=self._embedding_model_id(),
                    status=status,
                    enabled=status == "active",
                    source_type="auto_extract",
                    source_ref=session_id,
                    source_session_id=session_id,
                    source_turn=turn,
                    metadata={"provider_id": provider_id},
                )
                if memory_id not in written:
                    written.append(memory_id)
            state["last_memory_turn"] = turn
            state["last_memory_provider_id"] = provider_id
            return written
        except Exception as exc:
            logger.warning("%s 长期记忆提取失败，已跳过本轮: %s", PLUGIN_TAG, exc)
            return []

    async def _collect_entries(self, scopes: list[tuple[str, str]]) -> list[LoreEntry]:
        entries: list[LoreEntry] = []
        for kind in ("lorebook", "material"):
            documents = await asyncio.to_thread(self.storage.resolve_bindings, kind, scopes)
            for document in documents:
                normalized = normalize_entries(document["data"], kind=kind)
                for index, item in enumerate(normalized):
                    item.raw.setdefault("document_id", document["id"])
                    item.raw.setdefault("document_name", document.get("name", ""))
                    item.raw.setdefault("kind", kind)
                    item.raw.setdefault("index", index)
                entries.extend(normalized)
        return entries

    @staticmethod
    def _content_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    item_type = str(item.get("type", "") or "").lower()
                    if item_type in {"reasoning", "think", "thinking"}:
                        continue
                    if isinstance(item.get("message"), list):
                        parts.append(TavernService._content_text(item.get("message")))
                        continue
                    text = item.get("text")
                    if text is None and "content" in item:
                        text = item.get("content")
                    parts.append(TavernService._content_text(text))
                else:
                    parts.append(str(item))
            return "".join(part for part in parts if part)
        if isinstance(content, dict):
            content_type = str(content.get("type", "") or "").lower()
            if content_type in {"reasoning", "think", "thinking"}:
                return ""
            if isinstance(content.get("message"), list):
                return TavernService._content_text(content.get("message"))
            if "content" in content:
                nested = content.get("content")
                if nested is not content:
                    return TavernService._content_text(nested)
            if "text" in content:
                return TavernService._content_text(content.get("text"))
            if isinstance(content.get("message"), str):
                return str(content.get("message") or "")
        return str(content)

    @classmethod
    def _message_text(cls, message: dict[str, Any]) -> str:
        return cls._content_text(message.get("content", ""))

    @classmethod
    def _normalize_message(cls, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
        role = str(message.get("role", "") or "user")
        if not role:
            role = "user"
        return {"role": role, "content": cls._message_text(message)}

    @classmethod
    def _normalize_messages(cls, messages: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            normalized = cls._normalize_message(message)
            if normalized is not None:
                result.append(normalized)
        return result

    @staticmethod
    def _same_story_tail(left: str, right: str) -> bool:
        left_norm = " ".join(str(left or "").split())
        right_norm = " ".join(str(right or "").split())
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True
        left_tail, right_tail = left_norm[-800:], right_norm[-800:]
        return len(left_tail) >= 120 and (left_tail in right_norm or right_tail in left_norm)

    async def _conversation_boundary_changed(
        self,
        *,
        state: dict[str, Any],
        messages: list[dict[str, Any]],
        conversation_id: str,
    ) -> bool:
        previous_id = str(state.get("astrbot_conversation_id", "") or "")
        if previous_id and conversation_id and previous_id != conversation_id:
            return True
        node_id = str(state.get("current_story_node_id", "") or "")
        if not node_id:
            return False
        node = await asyncio.to_thread(self.storage.get_story_node, node_id)
        assistant_tail = str((node or {}).get("assistant_text", "") or "")
        if not assistant_tail:
            return False
        assistants = [self._message_text(item) for item in messages if item.get("role") == "assistant"]
        return not any(self._same_story_tail(assistant_tail, item) for item in assistants)

    @staticmethod
    def _fresh_conversation_state(conversation_id: str) -> dict[str, Any]:
        return {
            "turn": 0,
            "effects": {},
            "variables": {},
            "group_index": 0,
            "astrbot_conversation_id": conversation_id,
            "conversation_epoch": time.time(),
            "archive_new_root": True,
        }

    @staticmethod
    def _campaign_context(campaign: dict[str, Any] | None) -> str:
        if not campaign:
            return ""
        state = campaign.get("state_data", {}) if isinstance(campaign.get("state_data"), dict) else {}
        parts = [f"[当前战役：{campaign.get('name') or '未命名'}]"]
        if campaign.get("description"):
            parts.append(str(campaign["description"]))
        if campaign.get("rule_prompt"):
            parts.append("[本战役规则]\n" + str(campaign["rule_prompt"]))
        if state:
            parts.append("[当前权威状态；物品、任务和数值以此为准]\n" + json.dumps(state, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)

    @staticmethod
    def _apply_state_patch(state: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
        result = copy.deepcopy(state)
        for change in patch[:50]:
            if not isinstance(change, dict):
                continue
            op = str(change.get("op", "")).lower()
            path = [part for part in str(change.get("path", "")).split(".") if part]
            if not path or any(part.startswith("_") for part in path):
                continue
            cursor: dict[str, Any] = result
            for part in path[:-1]:
                value = cursor.get(part)
                if not isinstance(value, dict):
                    value = {}
                    cursor[part] = value
                cursor = value
            key = path[-1]
            value = copy.deepcopy(change.get("value"))
            if op == "set":
                cursor[key] = value
            elif op == "delete":
                cursor.pop(key, None)
            elif op == "increment" and isinstance(value, (int, float)):
                current = cursor.get(key, 0)
                if isinstance(current, (int, float)):
                    cursor[key] = current + value
            elif op == "append":
                current = cursor.setdefault(key, [])
                if isinstance(current, list) and value not in current:
                    current.append(value)
            elif op == "remove" and isinstance(cursor.get(key), list):
                cursor[key] = [item for item in cursor[key] if item != value]
        return result

    @staticmethod
    def _parse_state_change(text: str) -> tuple[list[dict[str, Any]], str]:
        raw = str(text or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end >= start:
            raw = raw[start:end + 1]
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [], ""
        patch = data.get("patch", []) if isinstance(data, dict) else []
        allowed = {"set", "delete", "increment", "append", "remove"}
        clean = [
            item for item in patch
            if isinstance(item, dict) and str(item.get("op", "")).lower() in allowed
            and str(item.get("path", "")).strip()
        ][:50]
        return clean, str(data.get("reason", "")) if isinstance(data, dict) else ""

    @staticmethod
    def _state_patch_risk(patch: list[dict[str, Any]]) -> str:
        high_prefixes = ("system.points", "resources", "equipment", "key_items", "condition.injuries", "condition.infection", "quests.completed", "quests.failed")
        medium_prefixes = ("condition", "quests", "base", "relationships", "inventory", "hp", "mp")
        paths = [str(item.get("path", "")) for item in patch if isinstance(item, dict)]
        if any(path == prefix or path.startswith(prefix + ".") for path in paths for prefix in high_prefixes):
            return "high"
        if any(path == prefix or path.startswith(prefix + ".") for path in paths for prefix in medium_prefixes):
            return "medium"
        return "low"

    async def _extract_campaign_state_change(
        self, *, campaign: dict[str, Any], session_id: str, turn: int,
        user_text: str, assistant_text: str, story_node_id: str,
    ) -> str:
        settings = campaign.get("settings", {}) if isinstance(campaign.get("settings"), dict) else {}
        if not settings.get("state_tracking_enabled", True):
            return ""
        interval = max(1, int(settings.get("state_extract_interval", 1) or 1))
        if turn % interval:
            return ""
        provider, _ = self._memory_provider(session_id)
        state = campaign.get("state_data", {}) if isinstance(campaign.get("state_data"), dict) else {}
        prompt = str(settings.get("state_prompt") or DEFAULT_CAMPAIGN_STATE_PROMPT)
        prompt = (prompt.replace("{schema}", json.dumps(campaign.get("state_schema", {}), ensure_ascii=False, indent=2))
                  .replace("{state}", json.dumps(state, ensure_ascii=False, indent=2))
                  .replace("{user}", user_text[-8000:]).replace("{assistant}", assistant_text[-12000:]))
        response, _ = await self._text_chat_with_fallback(
            session_id=session_id,
            provider=provider,
            prompt=prompt,
            max_tokens=768,
            temperature=0.0,
        )
        patch, reason = self._parse_state_change(str(getattr(response, "completion_text", "") or ""))
        if not patch:
            return ""
        after = self._apply_state_patch(state, patch)
        mode = str(settings.get("state_apply_mode", "pending") or "pending")
        risk_level = self._state_patch_risk(patch)
        status = "applied" if mode == "auto" or (mode == "tiered" and risk_level == "low") else "pending"
        change_id = await asyncio.to_thread(self.storage.put_campaign_state_change, {
            "campaign_id": campaign["id"], "session_id": session_id,
            "story_node_id": story_node_id, "source_turn": turn, "patch": patch,
            "reason": reason, "status": status, "risk_level": risk_level, "source_type": "llm",
            "before_state": state, "after_state": after,
        })
        if status == "applied":
            updated = dict(campaign)
            updated["state_data"] = after
            await asyncio.to_thread(self.storage.save_campaign, updated)
        return change_id

    @staticmethod
    def _story_context_from_node(node: dict[str, Any]) -> list[dict[str, Any]]:
        messages = node.get("request_messages") if isinstance(node.get("request_messages"), list) else []
        result: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            if role == "system" or message.get("_kt_injected") or message.get("_kt_example"):
                continue
            normalized = TavernService._normalize_message(message)
            if normalized is not None:
                result.append(normalized)
        assistant_text = str(node.get("assistant_text", "") or "").strip()
        if assistant_text:
            result.append({"role": "assistant", "content": assistant_text})
        return result

    @staticmethod
    def _conversation_history(conversation: Any) -> list[dict[str, Any]]:
        if conversation is None:
            return []
        raw = getattr(conversation, "history", conversation)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except (json.JSONDecodeError, TypeError):
                return []
        if not isinstance(raw, list):
            return []
        return [item for item in (TavernService._normalize_message(item) for item in raw) if item is not None]

    async def _web_contexts_for_session(self, session_id: str) -> tuple[list[dict[str, Any]], str, str]:
        manager = getattr(self.context, "conversation_manager", None)
        conversation_id = ""
        if manager is not None:
            try:
                conversation_id = str(await manager.get_curr_conversation_id(session_id) or "")
                if conversation_id:
                    conversation = await manager.get_conversation(session_id, conversation_id)
                    history = self._conversation_history(conversation)
                    if history:
                        return history, conversation_id, "conversation"
            except Exception as exc:
                logger.debug("%s 读取网页游玩 AstrBot conversation 失败: %s", PLUGIN_TAG, exc)
        state = await asyncio.to_thread(self.storage.get_session, session_id)
        node_id = str(state.get("current_story_node_id", "") or "")
        node = await asyncio.to_thread(self.storage.get_story_node, node_id) if node_id else None
        return (self._story_context_from_node(node) if node else []), conversation_id, "story" if node else "empty"

    async def _append_web_conversation(
        self, *, session_id: str, conversation_id: str, contexts: list[dict[str, Any]],
        user_text: str, assistant_text: str,
    ) -> bool:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None or not conversation_id:
            return False
        try:
            updated = list(contexts) + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
            await manager.update_conversation(session_id, conversation_id, updated)
            return True
        except Exception as exc:
            logger.debug("%s 写回网页游玩 AstrBot conversation 失败: %s", PLUGIN_TAG, exc)
            return False

    @classmethod
    def _message_fingerprint(cls, message: dict[str, Any]) -> str:
        payload = json.dumps(
            {"role": str(message.get("role", "")), "content": cls._message_text(message)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _unsummarized_history(
        self, messages: list[dict[str, Any]], state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        marker = str(state.get("history_summary", {}).get("covered_until", ""))
        if not marker:
            return list(messages)
        marker_index = -1
        for index, message in enumerate(messages):
            if self._message_fingerprint(message) == marker:
                marker_index = index
        return list(messages[marker_index + 1:]) if marker_index >= 0 else list(messages)

    async def _generate_history_summary(
        self,
        *,
        session_id: str,
        previous_summary: str,
        messages: list[dict[str, Any]],
    ) -> tuple[str, str]:
        configured_id = str(self.config.get("summary_provider_id", "") or "").strip()
        if configured_id:
            provider = self.context.get_provider_by_id(configured_id)
            if provider is None:
                raise RuntimeError(f"找不到摘要 Provider: {configured_id}")
            provider_id = configured_id
        else:
            provider = self.context.get_using_provider(session_id)
            if provider is None:
                raise RuntimeError("当前会话没有可用的摘要 Provider")
            provider_id = str(getattr(provider, "provider_config", {}).get("id", "current"))

        transcript = "\n".join(
            f"{str(message.get('role', 'unknown')).upper()}: {self._message_text(message)}"
            for message in messages
        )
        template = str(self.config.get("summary_prompt", DEFAULT_SUMMARY_PROMPT) or DEFAULT_SUMMARY_PROMPT)
        prompt = template.replace("{previous_summary}", previous_summary or "（无）").replace("{history}", transcript)
        timeout = max(1, int(self.config.get("summary_timeout_seconds", 60)))
        (response, provider_id) = await asyncio.wait_for(
            self._text_chat_with_fallback(
                session_id=session_id,
                provider=provider,
                prompt=prompt,
                max_tokens=max(128, int(self.config.get("summary_max_tokens", 1024))),
                temperature=0.2,
            ),
            timeout=timeout,
        )
        summary = str(getattr(response, "completion_text", "") or "").strip()
        if not summary:
            raise RuntimeError("摘要 Provider 返回空内容")
        return summary, provider_id

    async def _prepare_history(
        self,
        messages: list[dict[str, Any]],
        state: dict[str, Any],
        *,
        session_id: str,
        generate: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], bool]:
        enabled = bool(self.config.get("summary_enabled", False))
        saved = state.get("history_summary", {}) if isinstance(state.get("history_summary"), dict) else {}
        marker = str(saved.get("covered_until", "") or "")
        if marker and not any(self._message_fingerprint(message) == marker for message in messages):
            # AstrBot's /reset starts a fresh conversation while unified_msg_origin stays
            # unchanged.  The summary is keyed by that stable origin, so its boundary is
            # the reliable signal that the stored summary belongs to a different history.
            state.pop("history_summary", None)
            saved = {}
        previous_summary = str(saved.get("content", "") or "")
        unseen = self._unsummarized_history(messages, state) if enabled else list(messages)
        keep = max(1, int(self.config.get("history_max_messages", 12) or 12))
        trigger = max(keep + 1, int(self.config.get("summary_trigger_messages", 18)))
        should_generate = enabled and len(unseen) >= trigger
        warnings: list[str] = []
        generated = False
        failed = False
        error = ""
        provider_id = str(saved.get("provider_id", "") or "")
        covered_count = int(saved.get("covered_messages", 0) or 0)

        if should_generate and generate:
            batch = unseen[:-keep]
            try:
                summary, provider_id = await self._generate_history_summary(
                    session_id=session_id,
                    previous_summary=previous_summary,
                    messages=batch,
                )
                covered_count += len(batch)
                saved = {
                    "content": summary,
                    "covered_until": self._message_fingerprint(batch[-1]),
                    "covered_messages": covered_count,
                    "updated_at": time.time(),
                    "provider_id": provider_id,
                }
                state["history_summary"] = saved
                previous_summary = summary
                unseen = unseen[-keep:]
                generated = True
            except Exception as exc:
                failed = True
                error = str(exc) or type(exc).__name__
                warnings.append(f"自动摘要失败，已按普通裁剪继续：{error}")
                logger.warning("%s 自动摘要失败，已降级: %s", PLUGIN_TAG, error)
        elif should_generate:
            warnings.append("当前历史已达到自动摘要阈值；只读模拟不会调用摘要模型或推进摘要状态。")

        metadata = {
            "enabled": enabled,
            "source": "session" if previous_summary else "none",
            "content": previous_summary,
            "covered_messages": covered_count,
            "updated_at": saved.get("updated_at"),
            "generated_this_request": generated,
            "would_generate": should_generate and not generate,
            "pending_messages": len(unseen),
            "trigger_messages": trigger,
            "keep_messages": keep,
            "provider_id": provider_id,
            "error": error,
        }
        # Once summary mode has a valid rolling state, covered history is already
        # excluded. Before the first successful summary, retain legacy hard limits
        # on failure and in read-only simulations.
        skip_hard_limit = enabled and not failed and not (should_generate and not generate)
        return unseen, metadata, warnings, not skip_hard_limit

    @staticmethod
    def _serialize_result(
        result: BuildResult,
        scan: ScanResult,
        *,
        include_content: bool = False,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blocks = []
        for block in result.blocks:
            item = {
                "id": block.identifier,
                "name": block.name,
                "source": block.source,
                "role": block.role,
                "position": block.position,
                "depth": block.depth,
                "tokens": block.token_estimate,
            }
            if include_content:
                item["content"] = block.content
            blocks.append(item)

        activated = []
        for match in scan.activated:
            item = {
                "uid": match.entry.uid,
                "name": match.entry.comment,
                "reason": match.reason,
                "score": match.score,
                "step": match.recursion_step,
            }
            if include_content:
                item["content"] = match.entry.content
            activated.append(item)

        payload = {
            "messages": result.messages,
            "blocks": blocks,
            "dropped": result.dropped,
            "warnings": list(result.warnings),
            "activated": activated,
            "outlets": scan.outlets,
            "token_estimation": "approximate",
        }
        if summary is not None:
            summary_payload = dict(summary)
            if not include_content:
                summary_payload.pop("content", None)
            payload["summary"] = summary_payload
        return payload

    async def process(self, event: Any, req: Any, *, mode: str = "normal", quiet_prompt: str = ""):
        started = time.perf_counter()
        scopes = self.scopes(event, req)
        session_id = str(getattr(event, "unified_msg_origin", "") or req.session_id or "default")
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            campaign = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
            swipe_meta = event.get_extra("_kt_swipe_generation")
            if not isinstance(swipe_meta, dict):
                await asyncio.to_thread(
                    self.storage.close_candidate_groups, session_id
                )
                swipe_meta = {}
            base_state_snapshot = copy.deepcopy(state)
            base_campaign_snapshot = copy.deepcopy(
                (campaign or {}).get("state_data", {})
            )
            pending = state.pop("pending_generation", {})
            pending_branch = state.pop("pending_branch", {}) if isinstance(state.get("pending_branch"), dict) else {}
            source_contexts = self._normalize_messages(list(req.contexts or []))
            conversation_id = str(event.get_extra("_kt_astrbot_conversation_id") or "")
            skip_boundary = bool(event.get_extra("_kt_skip_boundary_check"))
            if not skip_boundary and not pending_branch and await self._conversation_boundary_changed(
                state=state, messages=source_contexts, conversation_id=conversation_id,
            ):
                state = self._fresh_conversation_state(conversation_id)
            elif conversation_id:
                state["astrbot_conversation_id"] = conversation_id
            generation_mode = str(event.get_extra("_kt_mode") or pending.get("mode") or mode)
            quiet_prompt = str(event.get_extra("_kt_quiet_prompt") or pending.get("prompt") or quiet_prompt)
            branch_parent_id = ""
            branch_name = ""
            branch_contexts: list[dict[str, Any]] | None = None
            if swipe_meta:
                branch_parent_id = str(
                    swipe_meta.get("parent_node_id", "") or ""
                )
            if pending_branch:
                branch_parent_id = str(pending_branch.get("source_node_id", "") or "")
                branch_name = str(pending_branch.get("branch_name", "") or "")
                node = await asyncio.to_thread(self.storage.get_story_node, branch_parent_id)
                if node:
                    node_state = copy.deepcopy(node.get("state_snapshot", {})) if isinstance(node.get("state_snapshot"), dict) else {}
                    campaign_state = node_state.pop("_campaign_state", None)
                    state.update(node_state)
                    applied_state = await asyncio.to_thread(
                        self.storage.applied_campaign_state_for_story_node, branch_parent_id,
                    )
                    if isinstance(applied_state, dict):
                        campaign_state = applied_state
                    if campaign and isinstance(campaign_state, dict):
                        campaign["state_data"] = campaign_state
                        await asyncio.to_thread(self.storage.save_campaign, campaign)
                    state["current_story_node_id"] = branch_parent_id
                    branch_contexts = self._story_context_from_node(node)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            entries = await self._collect_entries(scopes)
            source_contexts = branch_contexts if branch_contexts is not None else source_contexts
            scan_messages = list(source_contexts)
            if req.prompt:
                scan_messages.append({"role": "user", "content": req.prompt})

            retrieval_mode = str(self.config.get("retrieval_mode", "hybrid") or "hybrid")
            matcher = None
            if retrieval_mode == "vector" and not self.config.get("vector_enabled", False):
                pass
            elif retrieval_mode == "vector":
                matcher = self._vector_matcher
            else:
                matcher = self._hybrid_matcher

            scan = await self.scanner.scan(
                entries, scan_messages, state,
                vector_matcher=matcher,
            )
            preset_doc = await asyncio.to_thread(self._bound_one, "preset", scopes)
            character_doc, character_selection = await asyncio.to_thread(
                self._select_character_with_meta, scopes, req.prompt or "", state
            )
            persona_doc = await asyncio.to_thread(self._bound_one, "persona", scopes)
            history, summary_meta, summary_warnings, apply_history_limit = await self._prepare_history(
                list(source_contexts), state, session_id=session_id, generate=True
            )
            memory_text = "\n".join([self._message_text(item) for item in list(source_contexts)[-4:]])
            if req.prompt:
                memory_text = f"{memory_text}\n{req.prompt}".strip()
            memory_context, memory_matches = await self._retrieve_memories(scopes=scopes, text=memory_text)
            campaign_context = self._campaign_context(campaign)
            character = character_doc["data"] if character_doc else {}
            char_data = character.get("data", character)
            values = {
                "user": event.get_sender_name() or str(event.get_sender_id()),
                "char": char_data.get("name", "Assistant"),
                "persona": (persona_doc or {}).get("name", ""),
                "lastmessage": req.prompt or "",
                "original_system": req.system_prompt or "",
                "outlets": scan.outlets,
                **state.get("variables", {}),
            }
            result = self.builder.build(
                original_system=req.system_prompt or "", contexts=history,
                current_prompt=req.prompt or "", preset=preset_doc["data"] if preset_doc else {},
                character=character, persona=(persona_doc or {}).get("data", {}).get("content", ""),
                lore=scan, values=values, mode=generation_mode, quiet_prompt=quiet_prompt,
                session_summary=str(summary_meta.get("content", "")),
                memory_context=memory_context,
                campaign_context=campaign_context,
                apply_history_limit=apply_history_limit,
            )
            result.warnings.extend(summary_warnings)
            summary_meta["included"] = bool(summary_meta.get("content")) and any(
                block.identifier == "summary" and block.enabled and bool(block.content)
                for block in result.blocks
            )
            await self._extract_memories(
                session_id=session_id,
                state=state,
                messages=list(source_contexts) + ([{"role": "user", "content": req.prompt}] if req.prompt else []),
            )
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            preview = self._serialize_result(result, scan, summary=summary_meta)
            preview["character_selection"] = character_selection
            preview["memory"] = {
                "enabled": bool(self.config.get("memory_enabled", False)),
                "query": memory_text,
                "scopes": scopes,
                "injected_count": len(memory_matches),
                "matches": [
                    {key: item.get(key) for key in (
                        "id", "scope_type", "scope_id", "category", "content", "score",
                        "importance", "status", "source_type", "source_ref",
                        "source_session_id", "source_turn", "updated_at", "expires_at",
                    )}
                    for item in memory_matches
                ],
            }
            preview["campaign"] = {
                "id": str((campaign or {}).get("id", "")),
                "name": str((campaign or {}).get("name", "")),
                "state_data": copy.deepcopy((campaign or {}).get("state_data", {})),
            } if campaign else None
            preview["retrieval"] = {
                "enabled": bool(self.config.get("vector_enabled", False)) or retrieval_mode == "keyword",
                "mode": retrieval_mode,
                "fts_available": bool(self.storage.fts_available),
                "candidate_count": int(self.config.get("retrieval_candidate_k", 60)),
                "top_k": int(self.config.get("retrieval_top_k", 8)),
                "matches": [
                    {
                        "uid": match.entry.uid,
                        "name": match.entry.comment,
                        "score": match.score,
                        "reason": "matcher",
                        "scanner_reason": match.reason,
                    }
                    for match in scan.activated
                    if match.reason in ("vector", "hybrid", "keyword")
                ],
            }
            preview["cipher"] = self.cipher_metadata(provider_encoded=self.cipher_enabled)
            preview["url_request"] = self.url_request_metadata(
                provider_externalized=self.url_request_enabled,
            )
            await asyncio.to_thread(self.storage.save_preview, session_id, preview)

            if bool(self.config.get("archive_enabled", True)):
                effective = await asyncio.to_thread(self.effective_bindings, scopes)
                title = str(req.prompt or "").strip().splitlines()[0][:80]
                if not title:
                    title = f"第 {int(state.get('turn', 0) or 0)} 轮"
                event.set_extra("_kt_story_snapshot", {
                    "session_id": session_id,
                    "parent_id": branch_parent_id or str(state.get("current_story_node_id", "") or ""),
                    "branch_name": branch_name or ("新会话" if state.get("archive_new_root") else ""),
                    "title": title,
                    "turn_index": int(state.get("turn", 0) or 0),
                    "request_messages": result.messages,
                    "preview_payload": preview,
                    "bindings_snapshot": effective,
                    "retrieval_snapshot": preview.get("retrieval", {}),
                    "memory_snapshot": preview.get("memory", {}),
                    "state_snapshot": dict(copy.deepcopy(state), **({"_campaign_state": copy.deepcopy(campaign.get("state_data", {}))} if campaign else {})),
                    "base_state_snapshot": base_state_snapshot,
                    "base_campaign_snapshot": base_campaign_snapshot,
                    "candidate_group_id": str(swipe_meta.get("group_id", "") or ""),
                    "candidate_index": int(swipe_meta.get("candidate_index", 0) or 0),
                    "candidate_selected": bool(swipe_meta),
                })

            retrieval_matches = [
                match for match in scan.activated
                if match.reason in ("vector", "hybrid", "keyword")
            ]
            if retrieval_matches:
                entry_ids = []
                match_details = []
                for match in retrieval_matches:
                    entry_id = f"{match.entry.raw.get('document_id', '')}:{match.entry.uid}:{match.entry.raw.get('index', 0)}"
                    entry_ids.append(entry_id)
                    match_details.append({
                        "entry_id": entry_id,
                        "entry_uid": match.entry.uid,
                        "name": match.entry.comment,
                        "score": match.score,
                        "reason": "matcher",
                        "scanner_reason": match.reason,
                    })
                await asyncio.to_thread(
                    self.storage.record_retrieval_log,
                    session_id=session_id,
                    query=req.prompt or "",
                    mode=retrieval_mode,
                    matches=match_details,
                )
                await asyncio.to_thread(self.storage.increment_entry_match_counts, entry_ids)
            provider = getattr(req, "provider", None) or getattr(req, "llm_provider", None)
            provider_id = str(getattr(provider, "provider_config", {}).get("id", ""))
            if not provider_id:
                using = getattr(self.context, "get_using_provider", lambda _sid: None)(session_id)
                provider_id = str(getattr(using, "provider_config", {}).get("id", ""))
            provider_messages = self.provider_messages(result)
            url_overhead_tokens = (
                estimate_tokens(
                    url_request_overhead_text(
                        fetch_tool_enabled=self.url_requests.fetch_tool_enabled,
                        reply_tool_enabled=self.url_requests.reply_tool_enabled,
                    )
                )
                if self.url_request_enabled
                else 0
            )
            await asyncio.to_thread(self.storage.record_metric, {
                "session_id": session_id,
                "provider_id": provider_id,
                "mode": generation_mode,
                "prompt_tokens": sum(
                    estimate_tokens(str(message.get("content", "")))
                    for message in provider_messages
                ) + url_overhead_tokens,
                "message_count": len(provider_messages),
                "block_count": len(result.blocks),
                "worldbook_hits": len(scan.activated),
                "summary_generated": bool(summary_meta.get("generated_this_request")),
                "summary_failed": bool(summary_meta.get("error")),
                "memory_hits": len(memory_matches),
                "warning_count": len(result.warnings),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            })
        return result

    async def play_web_turn(
        self, *, session_id: str, prompt: str, mode: str = "normal", quiet_prompt: str = "",
        branch_node_id: str = "", branch_name: str = "",
        contexts_override: list[dict[str, Any]] | None = None,
        conversation_id_override: str = "",
        swipe_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        prompt = str(prompt or "").strip()
        mode = str(mode or "normal")
        if not session_id or not prompt:
            raise ValueError("请选择会话并输入玩家行动。")
        if mode not in {"normal", "continue", "impersonate", "quiet"}:
            raise ValueError("生成模式无效。")
        contexts, conversation_id, context_source = await self._web_contexts_for_session(session_id)
        if contexts_override is not None:
            contexts = copy.deepcopy(contexts_override)
            conversation_id = str(conversation_id_override or conversation_id)
            context_source = "swipe"
        if branch_node_id:
            node = await asyncio.to_thread(self.storage.get_story_node, branch_node_id)
            if not node:
                raise ValueError("找不到要继续的剧情节点。")
            contexts = self._story_context_from_node(node)
            context_source = "branch"
            await self.set_pending_branch(session_id, branch_node_id, branch_name)
        extras = {
            "_kt_astrbot_conversation_id": conversation_id,
            "_kt_mode": mode,
            "_kt_quiet_prompt": str(quiet_prompt or ""),
            "_kt_skip_boundary_check": context_source in {"story", "branch", "swipe"},
            "_kt_swipe_generation": copy.deepcopy(swipe_meta) if swipe_meta else None,
        }
        event = SimpleNamespace(
            unified_msg_origin=session_id,
            get_sender_name=lambda: "WebUI 玩家",
            get_sender_id=lambda: "webui",
            get_group_id=lambda: "",
        )
        event.get_extra = lambda key: extras.get(key)
        event.set_extra = lambda key, value: extras.__setitem__(key, value)
        provider = self.context.get_using_provider(session_id)
        req = SimpleNamespace(
            session_id=session_id,
            contexts=contexts,
            prompt=prompt,
            system_prompt="",
            provider=provider,
            llm_provider=provider,
            conversation=SimpleNamespace(persona_id=""),
        )
        result = await self.process(event, req, mode=mode, quiet_prompt=str(quiet_prompt or ""))
        if self.url_request_enabled:
            handle = await self.url_requests.create(result.messages)
            try:
                response, provider_id = await self._url_chat_completion_with_fallback(
                    session_id=session_id,
                    provider=provider,
                    handle=handle,
                )
            finally:
                await self.url_requests.delete(handle)
        else:
            response, provider_id = await self._chat_completion_with_fallback(
                session_id=session_id,
                provider=provider,
                messages=self.provider_messages(result),
            )
        raw_assistant_text = str(getattr(response, "completion_text", "") or "").strip()
        assistant_text, decoded, decode_error = self.decode_model_response(raw_assistant_text)
        assistant_text = strip_reasoning_tags(assistant_text)
        if not decoded:
            logger.warning(
                "%s 主模型密文回复自动解码失败（%s）: %s",
                PLUGIN_TAG, self.cipher.method, decode_error,
            )
        elif decode_error:
            logger.warning(
                "%s 主模型密文回复已降级恢复（%s）: %s",
                PLUGIN_TAG, self.cipher.method, decode_error,
            )
        if not assistant_text:
            raise RuntimeError("Provider 返回空回复。")
        snapshot = event.get_extra("_kt_story_snapshot")
        node_id = ""
        if isinstance(snapshot, dict):
            node_id = await self.finalize_story_snapshot(
                snapshot,
                assistant_text,
                {"completion_text": assistant_text, "provider_id": provider_id, "source": "webui"},
            )
        wrote_conversation = await self._append_web_conversation(
            session_id=session_id,
            conversation_id=conversation_id,
            contexts=contexts,
            user_text=prompt,
            assistant_text=assistant_text,
        )
        preview = await asyncio.to_thread(self.storage.get_preview, session_id)
        campaign = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
        changes = await asyncio.to_thread(
            self.storage.list_campaign_state_changes, str(campaign["id"]), None, 100
        ) if campaign else []
        return {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "provider_id": provider_id,
            "node_id": node_id,
            "reply": assistant_text,
            "conversation_synced": wrote_conversation,
            "preview": preview,
            "campaign": campaign,
            "changes": changes,
        }

    async def simulate(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "preview") or "preview")
        scopes = [("global", "*")]
        for scope_type, key in (("session", "session_id"), ("persona", "persona_id"),
                                ("user", "user_id"), ("group", "group_id")):
            value = str(payload.get(key, "") or "")
            if value:
                scopes.append((scope_type, value))
        campaign = await asyncio.to_thread(self.storage.campaign_for_session, session_id)
        if campaign:
            if campaign.get("world_id"):
                scopes.append(("world", str(campaign["world_id"])))
            if campaign.get("ruleset_id"):
                scopes.append(("ruleset", str(campaign["ruleset_id"])))
            scopes.append(("campaign", str(campaign["id"])))
        state = copy.deepcopy(await asyncio.to_thread(self.storage.get_session, session_id))
        messages = self._normalize_messages(list(payload.get("contexts", [])))
        history, summary_meta, summary_warnings, apply_history_limit = await self._prepare_history(
            messages, state, session_id=session_id, generate=False
        )
        prompt = str(payload.get("prompt", ""))
        scan_messages = messages + ([{"role": "user", "content": prompt}] if prompt else [])
        entries = await self._collect_entries(scopes)

        retrieval_mode = str(self.config.get("retrieval_mode", "hybrid") or "hybrid")
        matcher = None
        if retrieval_mode == "vector" and not self.config.get("vector_enabled", False):
            pass
        elif retrieval_mode == "vector":
            matcher = self._vector_matcher
        else:
            matcher = self._hybrid_matcher

        scan = await self.scanner.scan(
            entries, scan_messages, state,
            vector_matcher=matcher,
            rng=random.Random(int(payload.get("seed", 1))),
            )
        memory_text = "\n".join([self._message_text(item) for item in messages[-4:]])
        if prompt:
            memory_text = f"{memory_text}\n{prompt}".strip()
        memory_context, memory_matches = await self._retrieve_memories(scopes=scopes, text=memory_text)
        preset_doc = await asyncio.to_thread(self._bound_one, "preset", scopes)
        character_doc, character_selection = await asyncio.to_thread(
            self._select_character_with_meta, scopes, prompt, state
        )
        persona_doc = await asyncio.to_thread(self._bound_one, "persona", scopes)
        char_data = (character_doc or {}).get("data", {})
        char_values = char_data.get("data", char_data)
        values = {
            "user": str(payload.get("user_name", "User")),
            "char": str(char_values.get("name", "Assistant")),
            "persona": (persona_doc or {}).get("name", ""),
            "lastmessage": prompt,
            "original_system": str(payload.get("system_prompt", "")),
            "outlets": scan.outlets,
            **state.get("variables", {}),
        }
        result = self.builder.build(
            original_system=str(payload.get("system_prompt", "")), contexts=history,
            current_prompt=prompt, preset=(preset_doc or {}).get("data", {}),
            character=char_data, persona=(persona_doc or {}).get("data", {}).get("content", ""),
            lore=scan, values=values, mode=str(payload.get("mode", "normal")),
            quiet_prompt=str(payload.get("quiet_prompt", "")),
            session_summary=str(summary_meta.get("content", "")),
            memory_context=memory_context,
            campaign_context=self._campaign_context(campaign),
            apply_history_limit=apply_history_limit,
        )
        warnings = list(result.warnings) + summary_warnings
        summary_meta["included"] = bool(summary_meta.get("content")) and any(
            block.identifier == "summary" and block.enabled and bool(block.content)
            for block in result.blocks
        )
        if not preset_doc:
            warnings.append("当前作用域没有绑定提示词预设；原始 System Prompt 为空时，最终请求只会包含历史和用户消息。")
        if not character_doc:
            warnings.append("当前作用域没有绑定角色卡。")
        effective = await asyncio.to_thread(self.effective_bindings, scopes)
        serialized = self._serialize_result(result, scan, include_content=True, summary=summary_meta)
        serialized.update({
            "warnings": warnings,
            "effective": effective,
            "character_selection": character_selection,
            "state_after": state,
            "state_persisted": False,
            "memory": {
                "enabled": bool(self.config.get("memory_enabled", False)),
                "query": memory_text,
                "scopes": scopes,
                "injected_count": len(memory_matches),
                "matches": memory_matches,
            },
            "retrieval": {
                "enabled": bool(self.config.get("vector_enabled", False)) or retrieval_mode == "keyword",
                "mode": retrieval_mode,
                "fts_available": bool(self.storage.fts_available),
                "candidate_count": int(self.config.get("retrieval_candidate_k", 60)),
                "top_k": int(self.config.get("retrieval_top_k", 8)),
                "matches": [
                    {
                        "uid": match.entry.uid,
                        "name": match.entry.comment,
                        "score": match.score,
                        "reason": "matcher",
                        "scanner_reason": match.reason,
                    }
                    for match in scan.activated
                    if match.reason in ("vector", "hybrid", "keyword")
                ],
            },
            "cipher": self.cipher_metadata(provider_encoded=False),
            "url_request": self.url_request_metadata(
                provider_externalized=False,
            ),
        })
        return serialized

    async def test_retrieval(self, event: Any, text: str) -> str:
        if not text:
            return "请提供测试文本，例如：/tavern retrieval test 下雨的夜晚"

        scopes = self.scopes(event, None)
        entries = await self._collect_entries(scopes)
        if not entries:
            return "当前作用域没有绑定任何世界书或素材库。"

        mode = str(self.config.get("retrieval_mode", "hybrid") or "hybrid")
        scores = await self._hybrid_matcher(text, entries)

        if not scores:
            return f"检索模式：{mode}\n输入：{text}\n未命中任何条目。"

        entries_by_uid = {entry.uid: entry for entry in entries}
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        lines = [
            f"检索模式：{mode}",
            f"输入：{text}",
            f"命中 {len(sorted_scores)} 条：",
            "",
        ]

        for i, (uid, score) in enumerate(sorted_scores[:10], 1):
            entry = entries_by_uid.get(uid)
            if not entry:
                continue
            ext = entry.raw.get("extensions") if isinstance(entry.raw.get("extensions"), dict) else {}
            category = str(entry.raw.get("category", ext.get("category", entry.group or "")) or "")
            keywords = ", ".join(entry.keys[:5]) if entry.keys else "无"
            content_preview = entry.content[:80] + "..." if len(entry.content) > 80 else entry.content

            lines.append(f"{i}. [{score:.3f}] {entry.comment or uid}")
            if category:
                lines.append(f"   分类：{category}")
            lines.append(f"   关键词：{keywords}")
            lines.append(f"   预览：{content_preview}")
            lines.append("")

        return "\n".join(lines)

    async def get_retrieval_stats(self, session_id: str) -> dict[str, Any]:
        mode = str(self.config.get("retrieval_mode", "hybrid") or "hybrid")
        vector_enabled = bool(self.config.get("vector_enabled", False))

        stats = {
            "mode": mode,
            "vector_enabled": vector_enabled,
            "fts_available": bool(self.storage.fts_available),
            "embedding_provider": str(self.config.get("embedding_provider_id", "")),
            "keyword_weight": float(self.config.get("keyword_weight", 0.35)),
            "vector_weight": float(self.config.get("vector_weight", 0.65)),
            "retrieval_top_k": int(self.config.get("retrieval_top_k", 8)),
            "retrieval_candidate_k": int(self.config.get("retrieval_candidate_k", 60)),
            "category_dedup_limit": int(self.config.get("retrieval_category_dedup_limit", 2)),
        }

        try:
            with self.storage._connection() as conn:
                row = conn.execute("SELECT COUNT(*) as count FROM retrieval_logs").fetchone()
                stats["total_retrieval_logs"] = int(row["count"]) if row else 0

                row = conn.execute(
                    "SELECT COUNT(*) as count FROM retrieval_logs WHERE session_id=?",
                    (session_id,)
                ).fetchone()
                stats["session_retrieval_logs"] = int(row["count"]) if row else 0

                row = conn.execute("SELECT COUNT(*) as count FROM entry_index WHERE match_count > 0").fetchone()
                stats["entries_with_matches"] = int(row["count"]) if row else 0

                rows = conn.execute(
                    "SELECT name, match_count FROM entry_index WHERE match_count > 0 ORDER BY match_count DESC LIMIT 10"
                ).fetchall()
                stats["top_matched_entries"] = [
                    {"name": str(row["name"]), "match_count": int(row["match_count"])}
                    for row in rows
                ]
        except Exception as exc:
            stats["error"] = str(exc)

        return stats
