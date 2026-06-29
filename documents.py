from __future__ import annotations

import copy
from typing import Any


KINDS = {"preset", "character", "character_group", "persona", "lorebook", "material", "quick_reply"}
ROLES = {"system", "user", "assistant"}
POSITIONS = {"system", "examples", "depth"}
GENERATION_MODES = {"normal", "continue", "impersonate", "quiet"}


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
    elif kind == "character_group":
        result.setdefault("members", [])
        result.setdefault("selection", "round_robin")
    elif kind in {"lorebook", "material"}:
        result.setdefault("entries", [])
    elif kind == "quick_reply":
        result.setdefault("items", [])
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
    elif kind == "character_group":
        members = normalized.get("members")
        if not isinstance(members, list):
            errors.append("角色组 members 必须是数组")
        else:
            normalized["members"] = [str(item) for item in members if str(item).strip()]
        if str(normalized.get("selection", "round_robin")) not in {"round_robin", "manual"}:
            errors.append("角色组 selection 只能是 round_robin 或 manual")
    elif kind == "quick_reply":
        items = normalized.get("items")
        if not isinstance(items, list):
            errors.append("快捷回复 items 必须是数组")
        else:
            seen: set[str] = set()
            normalized_items = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"第 {index + 1} 个快捷回复不是对象")
                    continue
                label = str(item.get("label", "")).strip()
                content = str(item.get("content", "")).strip()
                if not label:
                    errors.append(f"第 {index + 1} 个快捷回复缺少名称")
                if not content:
                    warnings.append(f"快捷回复 {label or index + 1} 内容为空")
                alias = str(item.get("alias", "")).strip().lower()
                if alias and alias in seen:
                    warnings.append(f"快捷回复别名重复：{alias}")
                if alias:
                    seen.add(alias)
                mode = str(item.get("mode", "normal") or "normal")
                if mode not in GENERATION_MODES:
                    warnings.append(f"快捷回复 {label or index + 1} 的模式无效，已按 normal 处理")
                    mode = "normal"
                normalized_items.append({
                    "id": str(item.get("id", "")).strip() or f"qr_{index + 1}",
                    "label": label or f"快捷回复 {index + 1}",
                    "alias": str(item.get("alias", "")).strip(),
                    "content": content,
                    "mode": mode,
                    "enabled": bool(item.get("enabled", True)),
                    "append_input": bool(item.get("append_input", True)),
                    "order": int(item.get("order", index * 10) or 0),
                })
            normalized["items"] = normalized_items
    return normalized, errors, warnings
