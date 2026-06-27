from __future__ import annotations

import base64
import json
import sqlite3
import struct
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .documents import deep_merge


def parse_payload(content: str, file_name: str = "data.json") -> Any:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return yaml.safe_load(content)
    if suffix in {".txt", ".md"}:
        text = content.strip().lstrip("\ufeff")
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            text = text[1:-1].strip()
        if not text:
            raise ValueError("提示词文本为空")
        return {"name": Path(file_name).stem, "main_prompt": text, "blocks": []}
    return json.loads(content)


def parse_binary_payload(encoded: str, file_name: str) -> Any:
    raw = base64.b64decode(encoded, validate=True)
    if Path(file_name).suffix.lower() != ".png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("仅支持带角色卡元数据的 PNG 二进制导入")
    offset = 8
    while offset + 12 <= len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        chunk_type = raw[offset + 4:offset + 8]
        chunk_data = raw[offset + 8:offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"tEXt" and b"\x00" in chunk_data:
            keyword, value = chunk_data.split(b"\x00", 1)
            if keyword.lower() == b"chara":
                decoded = base64.b64decode(value).decode("utf-8")
                return json.loads(decoded)
        if chunk_type == b"IEND":
            break
    raise ValueError("PNG 中没有可识别的角色卡元数据")


def detect_kind(payload: Any) -> str:
    if isinstance(payload, dict):
        spec = str(payload.get("spec", "")).lower()
        if "chara_card" in spec or ("data" in payload and isinstance(payload.get("data"), dict)
                                    and "first_mes" in payload["data"]):
            return "character"
        entries = payload.get("entries")
        if isinstance(entries, (dict, list)):
            return "lorebook"
        if "prompts" in payload or "prompt_order" in payload or "main_prompt" in payload:
            return "preset"
    if isinstance(payload, list):
        return "material"
    return "document"


def preview_import(
    payload: Any,
    requested_kind: str | None = None,
    file_name: str | None = None,
) -> dict[str, Any]:
    kind = requested_kind or detect_kind(payload)
    fallback_name = Path(file_name).stem if file_name else "Imported"
    name = fallback_name
    count = 1
    warnings: list[str] = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        name = str(data.get("name", payload.get("name", fallback_name)) or fallback_name)
        entries = payload.get("entries")
        if isinstance(entries, (dict, list)):
            count = len(entries)
        if kind == "document":
            warnings.append("无法确定数据类型，将作为通用文档保存")
    return {"kind": kind, "name": name, "count": count, "warnings": warnings}


def read_material_sqlite(encoded: str) -> list[dict[str, Any]]:
    raw = base64.b64decode(encoded, validate=True)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        handle.write(raw)
        path = Path(handle.name)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            candidates = [name for name in tables if name.lower() in {"entries", "materials", "knowledge_entries"}]
            if not candidates:
                raise ValueError("数据库中没有可识别的素材表")
            table = candidates[0]
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if "content" not in columns:
                raise ValueError("素材表缺少 content 字段")
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            return [dict(row) for row in rows]
    finally:
        path.unlink(missing_ok=True)


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except Exception:
            pass
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def read_quill_kb_sqlite(encoded: str) -> list[dict[str, Any]]:
    raw = base64.b64decode(encoded, validate=True)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        handle.write(raw)
        path = Path(handle.name)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "knowledge_base" not in tables:
                raise ValueError("数据库中没有可识别的知识库表")
            rows = conn.execute('SELECT * FROM "knowledge_base"').fetchall()
            entries = []
            for index, row in enumerate(rows):
                row_dict = dict(row)
                keywords = _json_list(row_dict.get("keywords"))
                secondary = _json_list(row_dict.get("secondary_keywords"))
                aliases = _json_list(row_dict.get("aliases"))
                is_constant = bool(row_dict.get("is_constant"))
                priority = int(row_dict.get("priority") or 5)

                entry = {
                    "uid": str(row_dict.get("entry_id") or f"quill_{index}"),
                    "comment": str(row_dict.get("name") or row_dict.get("entry_id") or f"Knowledge Entry {index+1}"),
                    "content": str(row_dict.get("content") or ""),
                    "key": keywords + aliases,
                    "keysecondary": secondary,
                    "constant": is_constant,
                    "disabled": not bool(row_dict.get("enabled", True)),
                    "vectorized": True,
                    "order": max(1, 200 - priority * 10),
                    "position": 1 if is_constant else 4,
                    "useProbability": False,
                    "extensions": {
                        "category": str(row_dict.get("category") or ""),
                        "description": str(row_dict.get("description") or ""),
                        "aliases": aliases,
                        "source": "quill_kb",
                        "quill_priority": priority,
                        "quill_inject_position": int(row_dict.get("inject_position") or 2),
                    },
                }
                entries.append(entry)
            return entries
    finally:
        path.unlink(missing_ok=True)


def detect_quill_kb(encoded: str) -> bool:
    raw = base64.b64decode(encoded, validate=True)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        handle.write(raw)
        path = Path(handle.name)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return "knowledge_base" in tables
    except Exception:
        return False
    finally:
        path.unlink(missing_ok=True)


def export_document(document: dict[str, Any]) -> dict[str, Any]:
    raw = document.get("raw") or {}
    edited = document.get("data") or {}
    result = deep_merge(raw, edited)
    if isinstance(raw, dict) and "_komeiji_tavern_version" not in raw:
        result.pop("_komeiji_tavern_version", None)
    raw_entries = raw.get("entries") if isinstance(raw, dict) else None
    edited_entries = edited.get("entries") if isinstance(edited, dict) else None
    if isinstance(raw_entries, dict) and isinstance(edited_entries, list):
        rebuilt: dict[str, Any] = {}
        original_by_uid = {
            str(value.get("uid", value.get("id", key))): (str(key), value)
            for key, value in raw_entries.items() if isinstance(value, dict)
        }
        for index, value in enumerate(edited_entries):
            if not isinstance(value, dict):
                continue
            uid = str(value.get("uid", value.get("id", index)))
            original_key, original = original_by_uid.get(uid, (uid, {}))
            rebuilt[original_key] = deep_merge(original, value)
        result["entries"] = rebuilt
    return result
