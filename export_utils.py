from __future__ import annotations

import base64
import io
import json
import re
import time
import zipfile
from typing import Any, Iterable

from .constants import PLUGIN_VERSION
from .importers import export_document


KIND_DIRECTORIES = {
    "character": "角色卡",
    "preset": "提示词预设",
    "lorebook": "世界书",
    "persona": "用户设定",
    "character_group": "角色组",
    "material": "创作素材",
}


def safe_filename(value: Any, fallback: str = "export") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().rstrip(". ")
    return (name or fallback)[:120]


def download_payload(filename: str, mime: str, content: bytes) -> dict[str, str]:
    return {
        "filename": safe_filename(filename),
        "mime": mime,
        "base64": base64.b64encode(content).decode("ascii"),
    }


def document_download(document: dict[str, Any]) -> dict[str, str]:
    body = json.dumps(export_document(document), ensure_ascii=False, indent=2).encode("utf-8")
    return download_payload(f"{safe_filename(document.get('name'), 'document')}.json", "application/json", body)


def _write_json(archive: zipfile.ZipFile, path: str, value: Any) -> None:
    archive.writestr(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def build_document_archive(documents: Iterable[dict[str, Any]]) -> bytes:
    items = list(documents)
    manifest: dict[str, Any] = {
        "format": "komeiji-tavern-documents",
        "version": PLUGIN_VERSION,
        "exported_at": time.time(),
        "count": len(items),
        "items": [],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for document in items:
            kind = str(document.get("kind", "other"))
            document_id = str(document.get("id", ""))
            short_id = safe_filename(document_id[:8], "item")
            name = safe_filename(document.get("name"), kind)
            directory = KIND_DIRECTORIES.get(kind, "其他")
            path = f"{directory}/{name}-{short_id}.json"
            _write_json(archive, path, export_document(document))
            manifest["items"].append({
                "id": document_id,
                "kind": kind,
                "name": document.get("name", ""),
                "path": path,
            })
        _write_json(archive, "manifest.json", manifest)
    return output.getvalue()


def build_session_backup(
    session_id: str,
    state: dict[str, Any],
    preview: dict[str, Any] | None,
    story_nodes: list[dict[str, Any]] | None = None,
) -> bytes:
    preview = preview or {}
    story_nodes = story_nodes or []
    messages = preview.get("messages") if isinstance(preview.get("messages"), list) else []
    manifest = {
        "format": "komeiji-tavern-session-backup",
        "version": PLUGIN_VERSION,
        "exported_at": time.time(),
        "session_id": session_id,
        "has_preview": bool(preview),
        "message_count": len(messages),
        "story_node_count": len(story_nodes),
        "note": "此备份包含插件会话状态、请求预览和分支树节点，不包含 AstrBot 原始聊天记录。",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "manifest.json", manifest)
        _write_json(archive, "messages.json", messages)
        _write_json(archive, "preview.json", preview)
        _write_json(archive, "session-state.json", state)
        _write_json(archive, "story-nodes.json", story_nodes)
    return output.getvalue()
