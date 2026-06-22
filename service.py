from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import random
import json
import time
from collections import OrderedDict
from typing import Any

from astrbot.api import logger

from .lore import LoreScanner, normalize_entries
from .models import BuildResult, LoreEntry, ScanResult
from .prompt_builder import PromptBuilder
from .storage import TavernStorage

PLUGIN_TAG = "[Komeiji's Tavern]"
DEFAULT_SUMMARY_PROMPT = """请把以下旧聊天记录压缩成可供后续角色扮演继续使用的会话摘要。
保留人物关系、重要事实、事件顺序、承诺、状态变化、未解决事项和持续有效的偏好。
不要续写剧情，不要添加原文没有的信息，不要输出标题之外的解释。

已有摘要：
{previous_summary}

新增旧聊天记录：
{history}
"""


class TavernService:
    def __init__(self, storage: TavernStorage, context: Any, config: dict[str, Any]):
        self.storage = storage
        self.context = context
        self.config = config
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
        )
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_limit = 512
        self._session_locks: dict[str, asyncio.Lock] = {}

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

    @staticmethod
    def scopes(event: Any, req: Any) -> list[tuple[str, str]]:
        result = [("global", "*")]
        for scope_type, value in (
            ("session", getattr(event, "unified_msg_origin", "")),
            ("user", str(event.get_sender_id() or "")),
            ("group", str(event.get_group_id() or "")),
            ("persona", str(getattr(getattr(req, "conversation", None), "persona_id", "") or "")),
        ):
            if value:
                result.append((scope_type, value))
        return result

    def _bound_one(self, kind: str, scopes: list[tuple[str, str]]) -> dict[str, Any] | None:
        by_type = {scope_type: (scope_type, scope_id) for scope_type, scope_id in scopes}
        for scope_type in ("session", "persona", "user", "group", "global"):
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
            for kind in ("lorebook", "material")
        }
        return {"scopes": scopes, "single": single, "additive": additive}

    def _select_character(
        self, scopes: list[tuple[str, str]], prompt: str, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        group_doc = self._bound_one("character_group", scopes)
        if not group_doc:
            return self._bound_one("character", scopes)
        group = group_doc["data"]
        member_ids = [str(item) for item in group.get("members", [])]
        members = [self.storage.get_document(item) for item in member_ids]
        members = [item for item in members if item and item.get("kind") == "character"]
        if not members:
            return self._bound_one("character", scopes)
        lowered = prompt.lower()
        selected = next((item for item in members if item["name"].lower() in lowered), None)
        if selected is None:
            index = int(state.get("group_index", 0)) % len(members)
            selected = members[index]
            if group.get("selection", "round_robin") == "round_robin":
                state["group_index"] = (index + 1) % len(members)
        return selected

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

    async def _vector_matcher(self, text: str, entries: list[LoreEntry]) -> dict[str, float]:
        if not self.config.get("vector_enabled", False):
            return {}
        try:
            provider_id = str(self.config.get("embedding_provider_id", ""))
            providers = list(self.context.get_all_embedding_providers())
            provider = next((item for item in providers if str(item.provider_config.get("id", "")) == provider_id), None)
            if provider is None:
                return {}
            query = await provider.get_embedding(text)
            result: dict[str, float] = {}
            for entry in entries:
                key = hashlib.sha1(entry.content.encode("utf-8")).hexdigest()
                vector = self._cache_get(key)
                if vector is None:
                    vector = await provider.get_embedding(entry.content)
                    self._cache_put(key, vector)
                norm = math.sqrt(sum(x * x for x in query)) * math.sqrt(sum(x * x for x in vector))
                score = sum(a * b for a, b in zip(query, vector)) / norm if norm else 0.0
                threshold = float(entry.raw.get("vector_threshold", 0.35))
                if score >= threshold:
                    result[entry.uid] = score
            return result
        except Exception as exc:
            logger.warning("%s 向量匹配失败，已降级跳过向量条目: %s", PLUGIN_TAG, exc)
            return {}

    async def _collect_entries(self, scopes: list[tuple[str, str]]) -> list[LoreEntry]:
        entries: list[LoreEntry] = []
        for kind in ("lorebook", "material"):
            documents = await asyncio.to_thread(self.storage.resolve_bindings, kind, scopes)
            for document in documents:
                entries.extend(normalize_entries(document["data"]))
        return entries

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(
                str(item.get("text", item.get("content", ""))) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content)

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
        response = await asyncio.wait_for(
            provider.text_chat(
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
        scopes = self.scopes(event, req)
        session_id = str(getattr(event, "unified_msg_origin", "") or req.session_id or "default")
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            pending = state.pop("pending_generation", {})
            mode = str(event.get_extra("_kt_mode") or pending.get("mode") or mode)
            quiet_prompt = str(event.get_extra("_kt_quiet_prompt") or pending.get("prompt") or quiet_prompt)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            entries = await self._collect_entries(scopes)
            scan_messages = list(req.contexts or [])
            if req.prompt:
                scan_messages.append({"role": "user", "content": req.prompt})
            scan = await self.scanner.scan(
                entries, scan_messages, state,
                vector_matcher=self._vector_matcher if self.config.get("vector_enabled", False) else None,
            )
            preset_doc = await asyncio.to_thread(self._bound_one, "preset", scopes)
            character_doc = await asyncio.to_thread(self._select_character, scopes, req.prompt or "", state)
            persona_doc = await asyncio.to_thread(self._bound_one, "persona", scopes)
            history, summary_meta, summary_warnings, apply_history_limit = await self._prepare_history(
                list(req.contexts or []), state, session_id=session_id, generate=True
            )
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
                lore=scan, values=values, mode=mode, quiet_prompt=quiet_prompt,
                session_summary=str(summary_meta.get("content", "")),
                apply_history_limit=apply_history_limit,
            )
            result.warnings.extend(summary_warnings)
            summary_meta["included"] = bool(summary_meta.get("content")) and any(
                block.identifier == "summary" and block.enabled and bool(block.content)
                for block in result.blocks
            )
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            preview = self._serialize_result(result, scan, summary=summary_meta)
            await asyncio.to_thread(self.storage.save_preview, session_id, preview)
        return result

    async def simulate(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "preview") or "preview")
        scopes = [("global", "*")]
        for scope_type, key in (("session", "session_id"), ("persona", "persona_id"),
                                ("user", "user_id"), ("group", "group_id")):
            value = str(payload.get(key, "") or "")
            if value:
                scopes.append((scope_type, value))
        state = copy.deepcopy(await asyncio.to_thread(self.storage.get_session, session_id))
        messages = [item for item in payload.get("contexts", []) if isinstance(item, dict)]
        history, summary_meta, summary_warnings, apply_history_limit = await self._prepare_history(
            messages, state, session_id=session_id, generate=False
        )
        prompt = str(payload.get("prompt", ""))
        scan_messages = messages + ([{"role": "user", "content": prompt}] if prompt else [])
        entries = await self._collect_entries(scopes)
        scan = await self.scanner.scan(
            entries, scan_messages, state,
            vector_matcher=self._vector_matcher if self.config.get("vector_enabled", False) else None,
            rng=random.Random(int(payload.get("seed", 1))),
        )
        preset_doc = await asyncio.to_thread(self._bound_one, "preset", scopes)
        character_doc = await asyncio.to_thread(self._bound_one, "character", scopes)
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
            "state_after": state,
            "state_persisted": False,
        })
        return serialized
