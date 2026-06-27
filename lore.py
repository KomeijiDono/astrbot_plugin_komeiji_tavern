from __future__ import annotations

import random
import re
from typing import Any, Awaitable, Callable

from .models import ActivatedEntry, LoreEntry, Position, ScanResult, SelectiveLogic


VectorMatcher = Callable[[str, list[LoreEntry]], Awaitable[dict[str, float]]]


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def normalize_entry(raw: dict[str, Any], fallback_uid: str, *, kind: str = "lorebook") -> LoreEntry:
    ext = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    uid = str(raw.get("uid", raw.get("id", fallback_uid)))
    position = raw.get("position", ext.get("position"))

    if kind == "material":
        vectorized_default = True
        use_probability_default = False
        position_default = Position.AT_DEPTH
        order_default = 120
    else:
        vectorized_default = False
        use_probability_default = True
        position_default = Position.AFTER_CHARACTER
        order_default = 100

    probability = raw.get("probability", ext.get("probability", 100))
    return LoreEntry(
        uid=uid,
        content=str(raw.get("content", "")),
        comment=str(raw.get("comment", raw.get("title", ""))),
        keys=_list(raw.get("key", raw.get("keys", []))),
        secondary_keys=_list(raw.get("keysecondary", raw.get("secondary_keys", []))),
        constant=bool(raw.get("constant", False)),
        disabled=bool(raw.get("disable", raw.get("disabled", False))),
        selective=bool(raw.get("selective", False)),
        selective_logic=int(raw.get("selectiveLogic", raw.get("selective_logic", 0)) or 0),
        position=int(position if position is not None else position_default),
        depth=int(raw.get("depth", ext.get("depth", 4)) or 0),
        role=str(raw.get("role", ext.get("role", "system"))),
        order=int(raw.get("order", order_default) or order_default),
        probability=int(probability if probability is not None else 100),
        use_probability=bool(raw.get("useProbability", raw.get("use_probability", use_probability_default))),
        scan_depth=raw.get("scanDepth", ext.get("scan_depth")),
        case_sensitive=bool(raw.get("caseSensitive", ext.get("case_sensitive", False))),
        match_whole_words=bool(raw.get("matchWholeWords", ext.get("match_whole_words", False))),
        use_regex=bool(raw.get("useRegex", ext.get("use_regex", False))),
        sticky=int(raw.get("sticky", ext.get("sticky", 0)) or 0),
        cooldown=int(raw.get("cooldown", ext.get("cooldown", 0)) or 0),
        delay=int(raw.get("delay", ext.get("delay", 0)) or 0),
        exclude_recursion=bool(raw.get("excludeRecursion", ext.get("exclude_recursion", False))),
        prevent_recursion=bool(raw.get("preventRecursion", ext.get("prevent_recursion", False))),
        delay_until_recursion=bool(raw.get("delayUntilRecursion", ext.get("delay_until_recursion", False))),
        outlet_name=str(raw.get("outletName", ext.get("outlet_name", ""))),
        vectorized=bool(raw.get("vectorized", ext.get("vectorized", vectorized_default))),
        group=str(raw.get("group", "")),
        group_override=bool(raw.get("groupOverride", raw.get("group_override", False))),
        raw=dict(raw),
    )


def normalize_entries(document: dict[str, Any], *, kind: str = "lorebook") -> list[LoreEntry]:
    source = document.get("entries", document.get("data", document))
    if isinstance(source, dict):
        items = list(source.values())
    elif isinstance(source, list):
        items = source
    else:
        return []
    return [normalize_entry(item, str(index), kind=kind) for index, item in enumerate(items) if isinstance(item, dict)]


class LoreScanner:
    def __init__(self, *, default_scan_depth: int = 4, max_recursion_steps: int = 3):
        self.default_scan_depth = max(0, default_scan_depth)
        self.max_recursion_steps = max(0, max_recursion_steps)

    @staticmethod
    def _key_matches(text: str, key: str, entry: LoreEntry) -> bool:
        if not key:
            return False
        flags = 0 if entry.case_sensitive else re.IGNORECASE
        if entry.use_regex:
            try:
                return re.search(key, text, flags) is not None
            except re.error:
                return False
        candidate, needle = (text, key) if entry.case_sensitive else (text.lower(), key.lower())
        if entry.match_whole_words:
            return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", candidate) is not None
        return needle in candidate

    def _keyword_score(self, text: str, entry: LoreEntry) -> float:
        primary = sum(self._key_matches(text, key, entry) for key in entry.keys)
        if primary == 0:
            return 0.0
        if not entry.selective or not entry.secondary_keys:
            return float(primary)
        secondary = sum(self._key_matches(text, key, entry) for key in entry.secondary_keys)
        total = len(entry.secondary_keys)
        logic = SelectiveLogic(entry.selective_logic)
        passed = {
            SelectiveLogic.AND_ANY: secondary > 0,
            SelectiveLogic.AND_ALL: secondary == total,
            SelectiveLogic.NOT_ANY: secondary == 0,
            SelectiveLogic.NOT_ALL: secondary < total,
        }[logic]
        return float(primary + secondary) if passed else 0.0

    @staticmethod
    def _scan_text(messages: list[dict[str, Any]], depth: int) -> str:
        chunks: list[str] = []
        for message in reversed(messages[-max(0, depth):]):
            content = message.get("content", "") if isinstance(message, dict) else ""
            if isinstance(content, str):
                chunks.append(content)
        return "\n".join(chunks)

    async def scan(
        self,
        entries: list[LoreEntry],
        messages: list[dict[str, Any]],
        session_state: dict[str, Any],
        *,
        vector_matcher: VectorMatcher | None = None,
        rng: random.Random | None = None,
    ) -> ScanResult:
        rng = rng or random.Random()
        result = ScanResult()
        effects = session_state.setdefault("effects", {})
        turn = int(session_state.get("turn", 0)) + 1
        session_state["turn"] = turn
        activated_ids: set[str] = set()
        recursion_buffer: list[str] = []

        vector_scores: dict[str, float] = {}
        vector_entries = [entry for entry in entries if entry.vectorized and not entry.disabled]
        if vector_entries:
            if vector_matcher:
                vector_scores = await vector_matcher(self._scan_text(messages, self.default_scan_depth), vector_entries)
            else:
                result.warnings.append("向量条目已跳过：未配置可用的 Embedding Provider")

        for step in range(self.max_recursion_steps + 1):
            candidates: list[ActivatedEntry] = []
            for entry in entries:
                if entry.disabled or not entry.content or entry.uid in activated_ids:
                    continue
                effect = effects.setdefault(entry.uid, {})
                sticky_until = int(effect.get("sticky_until", 0))
                cooldown_until = int(effect.get("cooldown_until", 0))
                if sticky_until >= turn:
                    candidates.append(ActivatedEntry(entry, "sticky", 10_000, step))
                    continue
                if cooldown_until >= turn or turn <= entry.delay:
                    continue
                if step == 0 and entry.delay_until_recursion:
                    continue
                if step > 0 and entry.exclude_recursion:
                    continue
                depth = entry.scan_depth if entry.scan_depth is not None else self.default_scan_depth
                text = self._scan_text(messages, int(depth))
                if recursion_buffer:
                    text += "\n" + "\n".join(recursion_buffer)
                score = vector_scores.get(entry.uid, 0.0) if entry.vectorized else self._keyword_score(text, entry)
                reason = "vector" if entry.vectorized and score > 0 else "keyword"
                if entry.constant:
                    score, reason = 1.0, "constant"
                if score <= 0:
                    continue
                if entry.use_probability and rng.random() * 100 >= max(0, min(100, entry.probability)):
                    continue
                candidates.append(ActivatedEntry(entry, reason, score, step))

            candidates.sort(key=lambda item: (item.entry.order, -item.score, item.entry.uid))
            chosen: list[ActivatedEntry] = []
            groups: set[str] = set()
            for item in candidates:
                group = item.entry.group.strip()
                if group and group in groups and not item.entry.group_override:
                    continue
                if group:
                    groups.add(group)
                chosen.append(item)

            if not chosen:
                break
            stop_recursion = False
            for item in chosen:
                entry = item.entry
                activated_ids.add(entry.uid)
                result.activated.append(item)
                if entry.position == Position.OUTLET:
                    result.outlets.setdefault(entry.outlet_name or "default", []).append(entry.content)
                if not entry.exclude_recursion:
                    recursion_buffer.append(entry.content)
                if item.reason != "sticky":
                    sticky_until = turn + max(0, entry.sticky - 1)
                    cooldown_start = sticky_until if entry.sticky else turn
                    effects[entry.uid] = {
                        "activated_turn": turn,
                        "sticky_until": sticky_until,
                        "cooldown_until": cooldown_start + max(0, entry.cooldown),
                    }
                stop_recursion = stop_recursion or entry.prevent_recursion
            if stop_recursion:
                break

        return result

