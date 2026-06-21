from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import random
from collections import OrderedDict
from typing import Any

from astrbot.api import logger

from .lore import LoreScanner, normalize_entries
from .models import LoreEntry
from .prompt_builder import PromptBuilder
from .storage import TavernStorage

PLUGIN_TAG = "[Komeiji's Tavern]"


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
        )
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_limit = 512
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

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

    async def process(self, event: Any, req: Any, *, mode: str = "normal", quiet_prompt: str = ""):
        scopes = self.scopes(event, req)
        session_id = str(getattr(event, "unified_msg_origin", "") or req.session_id or "default")
        async with self._session_lock(session_id):
            state = await asyncio.to_thread(self.storage.get_session, session_id)
            pending = state.pop("pending_generation", {})
            mode = str(event.get_extra("_kt_mode") or pending.get("mode") or mode)
            quiet_prompt = str(event.get_extra("_kt_quiet_prompt") or pending.get("prompt") or quiet_prompt)
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            lore_documents = await asyncio.to_thread(self.storage.resolve_bindings, "lorebook", scopes)
            entries: list[LoreEntry] = []
            for document in lore_documents:
                entries.extend(normalize_entries(document["data"]))
            # Creative materials share the deterministic scanner and can therefore
            # use constants, keywords, probability, ordering, and lifecycle fields.
            material_documents = await asyncio.to_thread(self.storage.resolve_bindings, "material", scopes)
            for document in material_documents:
                entries.extend(normalize_entries(document["data"]))
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
                original_system=req.system_prompt or "", contexts=list(req.contexts or []),
                current_prompt=req.prompt or "", preset=preset_doc["data"] if preset_doc else {},
                character=character, persona=(persona_doc or {}).get("data", {}).get("content", ""),
                lore=scan, values=values, mode=mode, quiet_prompt=quiet_prompt,
            )
            await asyncio.to_thread(self.storage.save_session, session_id, state)
            await asyncio.to_thread(self.storage.save_preview, session_id, {
                "messages": result.messages,
                "blocks": [{"id": block.identifier, "name": block.name, "source": block.source,
                            "role": block.role, "position": block.position, "depth": block.depth,
                            "tokens": block.token_estimate} for block in result.blocks],
                "dropped": result.dropped, "warnings": result.warnings,
                "activated": [{"uid": item.entry.uid, "name": item.entry.comment,
                               "reason": item.reason, "score": item.score,
                               "step": item.recursion_step} for item in scan.activated],
                "outlets": scan.outlets, "token_estimation": "approximate",
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
        prompt = str(payload.get("prompt", ""))
        scan_messages = messages + ([{"role": "user", "content": prompt}] if prompt else [])
        entries: list[LoreEntry] = []
        lore_documents = await asyncio.to_thread(self.storage.resolve_bindings, "lorebook", scopes)
        for document in lore_documents:
            entries.extend(normalize_entries(document["data"]))
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
            original_system=str(payload.get("system_prompt", "")), contexts=messages,
            current_prompt=prompt, preset=(preset_doc or {}).get("data", {}),
            character=char_data, persona=(persona_doc or {}).get("data", {}).get("content", ""),
            lore=scan, values=values, mode=str(payload.get("mode", "normal")),
            quiet_prompt=str(payload.get("quiet_prompt", "")),
        )
        warnings = list(result.warnings)
        if not preset_doc:
            warnings.append("当前作用域没有绑定提示词预设；原始 System Prompt 为空时，最终请求只会包含历史和用户消息。")
        if not character_doc:
            warnings.append("当前作用域没有绑定角色卡。")
        effective = await asyncio.to_thread(self.effective_bindings, scopes)
        return {
            "messages": result.messages,
            "blocks": [{"id": block.identifier, "name": block.name, "source": block.source,
                        "role": block.role, "position": block.position, "depth": block.depth,
                        "tokens": block.token_estimate, "content": block.content}
                       for block in result.blocks],
            "dropped": result.dropped, "warnings": warnings,
            "activated": [{"uid": item.entry.uid, "name": item.entry.comment,
                           "reason": item.reason, "score": item.score,
                           "step": item.recursion_step, "content": item.entry.content}
                          for item in scan.activated],
            "outlets": scan.outlets, "effective": effective,
            "state_after": state, "state_persisted": False, "token_estimation": "approximate",
        }
