from __future__ import annotations

import copy
from typing import Any


KINDS = {"preset", "character", "character_group", "persona", "lorebook", "material"}
ROLES = {"system", "user", "assistant"}
POSITIONS = {"system", "examples", "depth"}


def deep_merge(base: Any, edited: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(edited, dict):
        return copy.deepcopy(edited)
    result = copy.deepcopy(base)
    for key, value in edited.items():
        result[key] = deep_merge(result.get(key), value) if key in result else copy.deepcopy(value)
    return result


def normalize_document(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(data)
    result.setdefault("_komeiji_tavern_version", 2)
    if kind == "character":
        card = result.setdefault("data", {}) if isinstance(result.get("data"), dict) else result
        card.setdefault("name", "未命名角色")
        for field in ("description", "personality", "scenario", "first_mes", "mes_example",
                      "system_prompt", "post_history_instructions"):
            card.setdefault(field, "")
    elif kind == "persona":
        result.setdefault("content", "")
    elif kind == "preset":
        result.setdefault("main_prompt", "{{original_system}}")
        result.setdefault("blocks", [])
        result.setdefault("allow_character_main_override", False)
        result.setdefault("allow_character_phi_override", True)
    elif kind in {"lorebook", "material"}:
        result.setdefault("entries", [])
    return result


def validate_document(kind: str, data: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if kind not in KINDS:
        errors.append(f"不支持的文档类型：{kind}")
    if not isinstance(data, dict):
        return {}, ["文档数据必须是 JSON 对象"], warnings
    normalized = normalize_document(kind, data)
    if kind == "preset":
        blocks = normalized.get("blocks")
        if not isinstance(blocks, list):
            errors.append("提示词块 blocks 必须是数组")
        else:
            identifiers: set[str] = set()
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    errors.append(f"第 {index + 1} 个提示词块不是对象")
                    continue
                identifier = str(block.get("identifier", block.get("id", ""))).strip()
                if not identifier:
                    errors.append(f"第 {index + 1} 个提示词块缺少标识")
                elif identifier in identifiers:
                    warnings.append(f"提示词块标识重复：{identifier}")
                identifiers.add(identifier)
                if str(block.get("role", "system")) not in ROLES:
                    errors.append(f"提示词块 {identifier or index + 1} 的角色无效")
                if str(block.get("position", "system")) not in POSITIONS:
                    errors.append(f"提示词块 {identifier or index + 1} 的注入位置无效")
    elif kind in {"lorebook", "material"}:
        entries = normalized.get("entries")
        if not isinstance(entries, (list, dict)):
            errors.append("entries 必须是数组或对象")
        else:
            values = entries.values() if isinstance(entries, dict) else entries
            for index, entry in enumerate(values):
                if not isinstance(entry, dict):
                    errors.append(f"第 {index + 1} 个条目不是对象")
                    continue
                probability = int(entry.get("probability", 100) or 0)
                if not 0 <= probability <= 100:
                    errors.append(f"第 {index + 1} 个条目的概率必须在 0 到 100 之间")
    elif kind == "character":
        card = normalized.get("data", normalized)
        if not str(card.get("name", "")).strip():
            errors.append("角色名称不能为空")
    return normalized, errors, warnings
