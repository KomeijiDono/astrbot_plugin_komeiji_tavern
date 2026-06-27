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
from .prompt_builder import PromptBuilder, estimate_tokens
from .storage import TavernStorage

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
            payload["state_snapshot"] = copy.deepcopy(state)
            node_id = await asyncio.to_thread(self.storage.create_story_node, payload)
            state["current_story_node_id"] = node_id
            await asyncio.to_thread(self.storage.save_session, session_id, state)
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

    def _embedding_provider(self):
        provider_id = str(self.config.get("embedding_provider_id", ""))
        providers = list(self.context.get_all_embedding_providers())
        return next((item for item in providers if str(item.provider_config.get("id", "")) == provider_id), None)

    async def _embedding(self, text: str) -> list[float]:
        provider = self._embedding_provider()
        if provider is None:
            return []
        return list(await provider.get_embedding(text))

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
            for scope_type, scope_id in scopes:
                candidates.extend(await asyncio.to_thread(
                    self.storage.list_memories,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    enabled=True,
                    limit=500,
                ))
            seen: set[str] = set()
            scored: list[tuple[float, dict[str, Any]]] = []
            for item in candidates:
                memory_id = str(item.get("id", ""))
                if not memory_id or memory_id in seen:
                    continue
                seen.add(memory_id)
                score = self._cosine(query, item.get("embedding", []))
                if score > 0:
                    scored.append((score, item))
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
            response = await provider.text_chat(
                prompt=prompt,
                max_tokens=max(128, int(self.config.get("memory_max_tokens", 512) or 512)),
                temperature=0.1,
            )
            items = self._parse_memory_items(str(getattr(response, "completion_text", "") or ""))
            written: list[str] = []
            for item in items:
                embedding = await self._embedding(item["content"])
                if not embedding:
                    continue
                memory_id = await asyncio.to_thread(
                    self.storage.put_memory,
                    scope_type="session",
                    scope_id=session_id,
                    category=item["category"],
                    content=item["content"],
                    embedding=embedding,
                    source_session_id=session_id,
                    source_turn=turn,
                )
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
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(
                str(item.get("text", item.get("content", ""))) if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content)

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
            result.append({"role": role or "user", "content": message.get("content", "")})
        assistant_text = str(node.get("assistant_text", "") or "").strip()
        if assistant_text:
            result.append({"role": "assistant", "content": assistant_text})
        return result

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
        started = time.perf_counter()
        scopes = self.scopes(event, req)
        session_id = str(getattr(event, "unified_msg_origin", "") or req.session_id or "default")
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            pending = state.pop("pending_generation", {})
            pending_branch = state.pop("pending_branch", {}) if isinstance(state.get("pending_branch"), dict) else {}
            generation_mode = str(event.get_extra("_kt_mode") or pending.get("mode") or mode)
            quiet_prompt = str(event.get_extra("_kt_quiet_prompt") or pending.get("prompt") or quiet_prompt)
            branch_parent_id = ""
            branch_name = ""
            branch_contexts: list[dict[str, Any]] | None = None
            if pending_branch:
                branch_parent_id = str(pending_branch.get("source_node_id", "") or "")
                branch_name = str(pending_branch.get("branch_name", "") or "")
                node = await asyncio.to_thread(self.storage.get_story_node, branch_parent_id)
                if node:
                    state.update(copy.deepcopy(node.get("state_snapshot", {})) if isinstance(node.get("state_snapshot"), dict) else {})
                    state["current_story_node_id"] = branch_parent_id
                    branch_contexts = self._story_context_from_node(node)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            entries = await self._collect_entries(scopes)
            source_contexts = branch_contexts if branch_contexts is not None else list(req.contexts or [])
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
            character_doc = await asyncio.to_thread(self._select_character, scopes, req.prompt or "", state)
            persona_doc = await asyncio.to_thread(self._bound_one, "persona", scopes)
            history, summary_meta, summary_warnings, apply_history_limit = await self._prepare_history(
                list(source_contexts), state, session_id=session_id, generate=True
            )
            memory_text = "\n".join([self._message_text(item) for item in list(source_contexts)[-4:]])
            if req.prompt:
                memory_text = f"{memory_text}\n{req.prompt}".strip()
            memory_context, memory_matches = await self._retrieve_memories(scopes=scopes, text=memory_text)
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
            preview["memory"] = {
                "enabled": bool(self.config.get("memory_enabled", False)),
                "matches": [
                    {key: item.get(key) for key in ("id", "scope_type", "scope_id", "category", "content", "score")}
                    for item in memory_matches
                ],
            }
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
            await asyncio.to_thread(self.storage.save_preview, session_id, preview)

            if bool(self.config.get("archive_enabled", True)):
                effective = await asyncio.to_thread(self.effective_bindings, scopes)
                title = str(req.prompt or "").strip().splitlines()[0][:80]
                if not title:
                    title = f"第 {int(state.get('turn', 0) or 0)} 轮"
                event.set_extra("_kt_story_snapshot", {
                    "session_id": session_id,
                    "parent_id": branch_parent_id or str(state.get("current_story_node_id", "") or ""),
                    "branch_name": branch_name,
                    "title": title,
                    "turn_index": int(state.get("turn", 0) or 0),
                    "request_messages": result.messages,
                    "preview_payload": preview,
                    "bindings_snapshot": effective,
                    "retrieval_snapshot": preview.get("retrieval", {}),
                    "memory_snapshot": preview.get("memory", {}),
                    "state_snapshot": copy.deepcopy(state),
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
            await asyncio.to_thread(self.storage.record_metric, {
                "session_id": session_id,
                "provider_id": provider_id,
                "mode": generation_mode,
                "prompt_tokens": sum(estimate_tokens(str(message.get("content", ""))) for message in result.messages),
                "message_count": len(result.messages),
                "block_count": len(result.blocks),
                "worldbook_hits": len(scan.activated),
                "summary_generated": bool(summary_meta.get("generated_this_request")),
                "summary_failed": bool(summary_meta.get("error")),
                "memory_hits": len(memory_matches),
                "warning_count": len(result.warnings),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            })
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
            memory_context=memory_context,
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
            "memory": {
                "enabled": bool(self.config.get("memory_enabled", False)),
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
