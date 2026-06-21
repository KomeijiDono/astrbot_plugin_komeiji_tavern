from __future__ import annotations

import copy
from typing import Any

from .macros import MacroResolver
from .models import BuildResult, LoreEntry, Position, PromptBlock, ScanResult


DEFAULT_ORDER = [
    "main", "world_before", "character", "personality", "scenario", "persona",
    "examples", "history", "author_note", "world_after", "summary", "memory",
    "post_history", "bias", "custom",
]


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_count = 0
    cjk_count = 0
    other = 0
    for char in text:
        cp = ord(char)
        if cp < 128:
            ascii_count += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF:
            cjk_count += 1
        else:
            other += 1
    return max(1, (ascii_count + 1) // 4 + cjk_count * 5 // 8 + other)


class PromptBuilder:
    def __init__(self, context_budget: int = 32768, output_reserve: int = 2048):
        self.context_budget = max(2048, context_budget)
        self.output_reserve = max(256, output_reserve)
        self.macros = MacroResolver()

    @staticmethod
    def _character_blocks(character: dict[str, Any]) -> dict[str, str]:
        data = character.get("data", character)
        return {
            "character": str(data.get("description", "")),
            "personality": str(data.get("personality", data.get("personality_summary", ""))),
            "scenario": str(data.get("scenario", "")),
            "examples": str(data.get("mes_example", "")),
            "post_history": str(data.get("post_history_instructions", "")),
        }

    @staticmethod
    def _lore_by_position(result: ScanResult, position: Position) -> str:
        values = [item.entry.content for item in result.activated if item.entry.position == position]
        return "\n\n".join(values)

    @staticmethod
    def _parse_examples(text: str, user_name: str, char_name: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        user_labels = {"user", user_name.lower()}
        char_labels = {"assistant", "char", char_name.lower()}
        for line in text.replace("<START>", "").splitlines():
            label, separator, content = line.partition(":")
            if not separator or not content.strip():
                continue
            normalized = label.strip().lower()
            role = "user" if normalized in user_labels else "assistant" if normalized in char_labels else ""
            if role:
                messages.append({"role": role, "content": content.strip(), "_kt_example": True})
        return messages

    def build(
        self,
        *,
        original_system: str,
        contexts: list[dict[str, Any]],
        current_prompt: str,
        preset: dict[str, Any] | None,
        character: dict[str, Any] | None,
        persona: str,
        lore: ScanResult,
        values: dict[str, Any],
        mode: str = "normal",
        quiet_prompt: str = "",
    ) -> BuildResult:
        preset = preset or {}
        char_blocks = self._character_blocks(character or {})
        card_data = (character or {}).get("data", character or {})
        character_main = str(card_data.get("system_prompt", ""))
        main_prompt = str(preset.get("main_prompt", original_system))
        if preset.get("allow_character_main_override", False) and character_main:
            main_prompt = character_main
        preset_phi = str(preset.get("post_history_instructions", ""))
        if not preset.get("allow_character_phi_override", True) or not char_blocks["post_history"]:
            char_blocks["post_history"] = preset_phi
        content_map = {
            "main": main_prompt,
            "world_before": self._lore_by_position(lore, Position.BEFORE_CHARACTER),
            **char_blocks,
            "persona": persona,
            "world_after": self._lore_by_position(lore, Position.AFTER_CHARACTER),
            "author_note": str(preset.get("author_note", "")),
            "summary": str(preset.get("summary", "")),
            "memory": str(preset.get("memory", "")),
            "custom": str(preset.get("custom", "")),
            "bias": str(preset.get("bias", "")),
        }
        if original_system and preset.get("main_prompt"):
            content_map["astrbot_system"] = original_system

        configured = preset.get("blocks") if isinstance(preset.get("blocks"), list) else []
        if configured:
            blocks = [PromptBlock(
                identifier=str(item.get("identifier", item.get("id", "custom"))),
                name=str(item.get("name", item.get("identifier", "Prompt"))),
                content=str(item.get("content", content_map.get(str(item.get("identifier", "")), ""))),
                role=str(item.get("role", "system")), enabled=bool(item.get("enabled", True)),
                position=str(item.get("position", "examples" if item.get("identifier") == "examples"
                                      else "depth" if item.get("identifier") == "post_history" else "system")),
                depth=int(item.get("depth", 0)),
                priority=int(item.get("priority", 50)), source=str(item.get("source", "preset")), raw=item,
            ) for item in configured]
        else:
            order = list(DEFAULT_ORDER)
            if "astrbot_system" in content_map:
                order.insert(1, "astrbot_system")
            blocks = [PromptBlock(key, key.replace("_", " ").title(), content_map.get(key, ""),
                                  position="examples" if key == "examples" else "depth" if key == "post_history" else "system",
                                  priority=index * 5, source="built-in")
                      for index, key in enumerate(order) if key != "history"]

        # Explicit position entries are messages rather than system string fragments.
        for item in lore.activated:
            entry = item.entry
            if entry.position == Position.AT_DEPTH:
                blocks.append(PromptBlock(
                    f"lore:{entry.uid}", entry.comment or entry.uid, entry.content,
                    role=entry.role if entry.role in {"system", "user", "assistant"} else "system",
                    position="depth", depth=max(0, entry.depth), priority=entry.order,
                    source=f"lore:{item.reason}", raw=entry.raw,
                ))
            elif entry.position in {Position.AUTHOR_NOTE_TOP, Position.AUTHOR_NOTE_BOTTOM,
                                    Position.EXAMPLE_TOP, Position.EXAMPLE_BOTTOM}:
                target = "author_note" if entry.position in {Position.AUTHOR_NOTE_TOP, Position.AUTHOR_NOTE_BOTTOM} else "examples"
                before = entry.position in {Position.AUTHOR_NOTE_TOP, Position.EXAMPLE_TOP}
                existing = next((block for block in blocks if block.identifier == target), None)
                if existing:
                    existing.content = f"{entry.content}\n\n{existing.content}" if before else f"{existing.content}\n\n{entry.content}"
                else:
                    blocks.append(PromptBlock(target, target, entry.content, priority=entry.order, source="lore"))

        if mode == "continue":
            blocks.append(PromptBlock("continue", "Continue", "Continue the last assistant response without repeating it.", priority=0))
        elif mode == "impersonate":
            blocks.append(PromptBlock("impersonate", "Impersonate", "Write the next user message in the user's voice. Output only the message.", priority=0))
        if quiet_prompt:
            blocks.append(PromptBlock("quiet", "Quiet Prompt", quiet_prompt, role="user", position="depth", depth=0, priority=0))

        for block in blocks:
            block.content = self.macros.render(block.content, values).strip()
            block.token_estimate = estimate_tokens(block.content)

        active = [block for block in blocks if block.enabled and block.content]
        history = copy.deepcopy(contexts or [])
        available = self.context_budget - self.output_reserve - estimate_tokens(current_prompt)
        total = sum(block.token_estimate for block in active) + sum(estimate_tokens(str(m.get("content", ""))) for m in history)
        dropped: list[str] = []

        for block in sorted(active, key=lambda value: value.priority, reverse=True):
            if total <= available:
                break
            if block.priority <= 10:
                continue
            active.remove(block)
            total -= block.token_estimate
            dropped.append(block.identifier)
        while history and total > available:
            removed = history.pop(0)
            total -= estimate_tokens(str(removed.get("content", "")))
            dropped.append("history:oldest")

        system_blocks = [block for block in active if block.position == "system" and block.role == "system"]
        system_prompt = "\n\n".join(block.content for block in system_blocks)
        example_blocks = [block for block in active if block.position == "examples"]
        examples: list[dict[str, str]] = []
        for block in example_blocks:
            examples.extend(self._parse_examples(block.content, str(values.get("user", "user")), str(values.get("char", "assistant"))))
        history = examples + history
        depth_blocks = [block for block in active if block.position == "depth" or (block.role != "system" and block.position != "examples")]
        for block in sorted(depth_blocks, key=lambda value: (value.depth, value.priority), reverse=True):
            index = max(0, len(history) - block.depth)
            history.insert(index, {"role": block.role, "content": block.content, "_kt_injected": block.identifier})

        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + history
        if current_prompt:
            messages.append({"role": "user", "content": current_prompt})
        return BuildResult(system_prompt, history, active, dropped, list(lore.warnings), messages)
