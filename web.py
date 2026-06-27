from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from quart import Response, jsonify, request

from .constants import API_PREFIX, PLUGIN_VERSION
from .importers import (
    detect_quill_kb,
    export_document,
    parse_binary_payload,
    parse_payload,
    preview_import,
    read_material_sqlite,
    read_quill_kb_sqlite,
)
from .documents import normalize_document, validate_document
from .export_utils import (
    build_document_archive,
    build_session_backup,
    document_download,
    download_payload,
    safe_filename,
)
from .service import TavernService
from .storage import TavernStorage


Handler = Callable[..., Awaitable[Any]]


class TavernWebApi:
    PREFIX = API_PREFIX

    def __init__(self, storage: TavernStorage, service: TavernService, context: Any, static_dir: str | Path):
        self.storage = storage
        self.service = service
        self.context = context
        self.static_dir = Path(static_dir)

    def routes(self) -> list[tuple[str, list[str], Handler, str]]:
        return [
            (f"{self.PREFIX}/panel", ["GET"], self.panel, "Management panel"),
            (f"{self.PREFIX}/static/<path:file_name>", ["GET"], self.static, "Panel assets"),
            (f"{self.PREFIX}/assets/<path:file_name>", ["GET"], self.asset, "Panel assets"),
            (f"{self.PREFIX}/documents", ["GET"], self.list_documents, "List documents"),
            (f"{self.PREFIX}/documents", ["POST"], self.save_document, "Create or update document"),
            (f"{self.PREFIX}/documents/validate", ["POST"], self.validate, "Validate document"),
            (f"{self.PREFIX}/documents/duplicate", ["POST"], self.duplicate, "Duplicate document"),
            (f"{self.PREFIX}/documents/<document_id>", ["GET"], self.get_document, "Get document"),
            (f"{self.PREFIX}/documents/<document_id>", ["DELETE"], self.delete_document, "Delete document"),
            (f"{self.PREFIX}/documents/delete", ["POST"], self.delete_document_post, "Delete document"),
            (f"{self.PREFIX}/bindings", ["POST"], self.bind, "Create binding"),
            (f"{self.PREFIX}/bindings", ["GET"], self.list_bindings, "List bindings"),
            (f"{self.PREFIX}/bindings/effective", ["POST"], self.effective_bindings, "Resolve bindings"),
            (f"{self.PREFIX}/bindings/delete", ["POST"], self.unbind, "Delete binding"),
            (f"{self.PREFIX}/import/preview", ["POST"], self.import_preview, "Preview import"),
            (f"{self.PREFIX}/import/commit", ["POST"], self.import_commit, "Commit import"),
            (f"{self.PREFIX}/import/sqlite", ["POST"], self.import_sqlite, "Import material database"),
            (f"{self.PREFIX}/export/<document_id>", ["GET"], self.export, "Export document"),
            (f"{self.PREFIX}/export/document", ["POST"], self.export_document_download, "Export document download"),
            (f"{self.PREFIX}/export/archive", ["POST"], self.export_archive, "Export document archive"),
            (f"{self.PREFIX}/memories", ["GET"], self.memories, "List long-term memories"),
            (f"{self.PREFIX}/memories", ["POST"], self.save_memory, "Create or update long-term memory"),
            (f"{self.PREFIX}/memories/<memory_id>/toggle", ["POST"], self.toggle_memory, "Toggle long-term memory"),
            (f"{self.PREFIX}/memories/<memory_id>/delete", ["POST"], self.delete_memory, "Delete long-term memory"),
            (f"{self.PREFIX}/metrics", ["GET"], self.metrics, "Runtime metrics"),
            (f"{self.PREFIX}/retrieval/test", ["POST"], self.retrieval_test, "Test retrieval"),
            (f"{self.PREFIX}/retrieval/stats", ["GET"], self.retrieval_stats, "Retrieval stats"),
            (f"{self.PREFIX}/archive", ["GET"], self.archive_nodes, "List story nodes"),
            (f"{self.PREFIX}/archive/export", ["POST"], self.archive_export, "Export story nodes"),
            (f"{self.PREFIX}/archive/<node_id>", ["GET"], self.archive_node, "Get story node"),
            (f"{self.PREFIX}/archive/<node_id>/rename", ["POST"], self.archive_rename, "Rename story node"),
            (f"{self.PREFIX}/archive/<node_id>/branch", ["POST"], self.archive_branch, "Continue from story node"),
            (f"{self.PREFIX}/preview/<session_id>", ["GET"], self.preview, "Last request preview"),
            (f"{self.PREFIX}/session/<session_id>", ["GET"], self.session, "Session state"),
            (f"{self.PREFIX}/session/<session_id>/reset", ["POST"], self.reset_session, "Reset session"),
            (f"{self.PREFIX}/session/<session_id>/backup", ["POST"], self.backup_session, "Backup session"),
            (f"{self.PREFIX}/generation", ["POST"], self.generation, "Set next generation mode"),
            (f"{self.PREFIX}/overview", ["GET"], self.overview, "Configuration overview"),
            (f"{self.PREFIX}/catalog/personas", ["GET"], self.personas, "AstrBot personas"),
            (f"{self.PREFIX}/catalog/conversations", ["GET"], self.conversations, "AstrBot conversations"),
            (f"{self.PREFIX}/simulate", ["POST"], self.simulate, "Simulate final request"),
        ]

    @staticmethod
    def ok(data: Any = None, **extra: Any):
        return jsonify({"status": "ok", "data": data, **extra})

    @staticmethod
    def error(message: str, status: int = 400):
        return jsonify({"status": "error", "message": message}), status

    async def panel(self):
        path = self.static_dir / "index.html"
        if not path.exists():
            return self.error("Frontend has not been built", 503)
        return Response(path.read_text(encoding="utf-8"), content_type="text/html")

    async def static(self, file_name: str):
        root = self.static_dir.resolve()
        path = (root / file_name).resolve()
        if root not in path.parents or not path.is_file():
            return self.error("Asset not found", 404)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(path.read_bytes(), content_type=content_type)

    async def asset(self, file_name: str):
        return await self.static(f"assets/{file_name}")

    async def list_documents(self):
        kind = request.args.get("kind")
        return self.ok(self.storage.list_documents(kind))

    async def get_document(self, document_id: str):
        document = self.storage.get_document(document_id)
        return self.ok(document) if document else self.error("Document not found", 404)

    async def save_document(self):
        payload = await request.get_json(force=True)
        if not isinstance(payload, dict):
            return self.error("JSON object required")
        kind, name, data = payload.get("kind"), payload.get("name"), payload.get("data")
        if not kind or not name or not isinstance(data, dict):
            return self.error("kind, name and object data are required")
        normalized, errors, warnings = validate_document(str(kind), data)
        if errors:
            return self.error("；".join(errors))
        existing = self.storage.get_document(str(payload.get("id", ""))) if payload.get("id") else None
        raw = payload.get("raw") or (existing or {}).get("raw") or data
        document_id = self.storage.put_document(
            str(kind), str(name), normalized, document_id=payload.get("id"), raw=raw
        )
        return self.ok({"id": document_id, "warnings": warnings})

    async def validate(self):
        payload = await request.get_json(force=True)
        normalized, errors, warnings = validate_document(str(payload.get("kind", "")), payload.get("data"))
        return self.ok({"valid": not errors, "normalized": normalized,
                        "errors": errors, "warnings": warnings})

    async def duplicate(self):
        payload = await request.get_json(force=True)
        document_id = self.storage.duplicate_document(str(payload.get("id", "")), payload.get("name"))
        return self.ok({"id": document_id}) if document_id else self.error("文档不存在", 404)

    async def delete_document(self, document_id: str):
        return self.ok({"deleted": self.storage.delete_document(document_id)})

    async def delete_document_post(self):
        payload = await request.get_json(force=True)
        document_id = str((payload or {}).get("id", ""))
        if not document_id:
            return self.error("Document id required")
        return self.ok({"deleted": self.storage.delete_document(document_id)})

    async def bind(self):
        payload = await request.get_json(force=True)
        required = ("scope_type", "scope_id", "kind", "target_id")
        if not isinstance(payload, dict) or not all(payload.get(key) for key in required):
            return self.error("Missing binding fields")
        self.storage.bind(*(str(payload[key]) for key in required), int(payload.get("priority", 0)))
        return self.ok()

    async def list_bindings(self):
        return self.ok(self.storage.list_bindings(
            scope_type=request.args.get("scope_type"), scope_id=request.args.get("scope_id"),
            kind=request.args.get("kind"), target_id=request.args.get("target_id"),
        ))

    async def effective_bindings(self):
        payload = await request.get_json(force=True)
        scopes = [("global", "*")]
        for scope_type, key in (("session", "session_id"), ("persona", "persona_id"),
                                ("user", "user_id"), ("group", "group_id")):
            value = str(payload.get(key, "") or "")
            if value:
                scopes.append((scope_type, value))
        return self.ok(self.service.effective_bindings(scopes))

    async def unbind(self):
        payload = await request.get_json(force=True)
        required = ("scope_type", "scope_id", "kind", "target_id")
        if not isinstance(payload, dict) or not all(payload.get(key) for key in required):
            return self.error("Missing binding fields")
        self.storage.unbind(*(str(payload[key]) for key in required))
        return self.ok()

    async def import_preview(self):
        payload = await request.get_json(force=True)
        try:
            parsed = (parse_binary_payload(str(payload.get("base64", "")), str(payload.get("file_name", "data.png")))
                      if payload.get("base64") else
                      parse_payload(str(payload.get("content", "")), str(payload.get("file_name", "data.json"))))
            return self.ok({
                "preview": preview_import(parsed, payload.get("kind"), payload.get("file_name")),
                "parsed": parsed,
            })
        except Exception as exc:
            return self.error(str(exc))

    async def import_commit(self):
        payload = await request.get_json(force=True)
        try:
            parsed = payload.get("parsed")
            if parsed is None:
                parsed = (parse_binary_payload(str(payload.get("base64", "")), str(payload.get("file_name", "data.png")))
                          if payload.get("base64") else
                          parse_payload(str(payload.get("content", "")), str(payload.get("file_name", "data.json"))))
            info = preview_import(parsed, payload.get("kind"), payload.get("file_name"))
            normalized, errors, warnings = validate_document(info["kind"], parsed)
            if errors:
                return self.error("；".join(errors))
            document_id = self.storage.put_document(info["kind"], payload.get("name") or info["name"], normalized, raw=parsed)
            return self.ok({"id": document_id, **info, "warnings": info.get("warnings", []) + warnings})
        except Exception as exc:
            return self.error(str(exc))

    async def import_sqlite(self):
        payload = await request.get_json(force=True)
        try:
            base64_data = str(payload.get("base64", ""))
            fmt = str(payload.get("format", "")).lower()
            name = str(payload.get("name", ""))

            if fmt == "quill_kb":
                entries = read_quill_kb_sqlite(base64_data)
                if not name:
                    name = "Imported Knowledge Base"
            elif fmt == "material":
                entries = read_material_sqlite(base64_data)
                if not name:
                    name = "Imported Materials"
            else:
                if detect_quill_kb(base64_data):
                    entries = read_quill_kb_sqlite(base64_data)
                    fmt = "quill_kb"
                    if not name:
                        name = "Imported Knowledge Base"
                else:
                    entries = read_material_sqlite(base64_data)
                    fmt = "material"
                    if not name:
                        name = "Imported Materials"

            data = {"entries": entries}
            document_id = self.storage.put_document("material", name, data, raw=data)
            return self.ok({"id": document_id, "count": len(entries), "format": fmt})
        except Exception as exc:
            return self.error(str(exc))

    async def export(self, document_id: str):
        document = self.storage.get_document(document_id)
        if not document:
            return self.error("Document not found", 404)
        body = json.dumps(export_document(document), ensure_ascii=False, indent=2)
        return Response(body, content_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{document_id}.json"'})

    async def export_document_download(self):
        payload = await request.get_json(force=True)
        document = self.storage.get_document(str((payload or {}).get("id", "")))
        return self.ok(document_download(document)) if document else self.error("资料不存在", 404)

    async def export_archive(self):
        payload = await request.get_json(force=True)
        payload = payload if isinstance(payload, dict) else {}
        kinds = {str(value) for value in payload.get("kinds", []) if value}
        ids = {str(value) for value in payload.get("ids", []) if value}
        documents = self.storage.list_documents()
        if kinds:
            documents = [item for item in documents if item.get("kind") in kinds]
        if ids:
            documents = [item for item in documents if item.get("id") in ids]
        if not documents:
            return self.error("没有可导出的资料", 404)
        name = safe_filename(payload.get("name"), "komeiji-tavern-documents")
        content = build_document_archive(documents)
        return self.ok(download_payload(f"{name}.zip", "application/zip", content))

    async def memories(self):
        enabled_arg = request.args.get("enabled")
        enabled = None if enabled_arg in {None, ""} else enabled_arg not in {"0", "false", "False"}
        return self.ok(self.storage.list_memories(
            scope_type=request.args.get("scope_type"),
            scope_id=request.args.get("scope_id"),
            enabled=enabled,
            query=request.args.get("q"),
            limit=int(request.args.get("limit", 300)),
        ))

    async def save_memory(self):
        payload = await request.get_json(force=True)
        if not isinstance(payload, dict):
            return self.error("JSON object required")
        content = str(payload.get("content", "")).strip()
        scope_type = str(payload.get("scope_type", "session") or "session").strip()
        scope_id = str(payload.get("scope_id", "")).strip()
        if not content or not scope_type or not scope_id:
            return self.error("content, scope_type and scope_id are required")
        warning = ""
        try:
            embedding = await self.service._embedding(content)
            if not embedding:
                warning = "Embedding Provider 未配置或不可用；该记忆已保存，但暂时不会参与向量检索。"
        except Exception as exc:
            embedding = []
            warning = f"生成 embedding 失败：{exc}"
        memory_id = self.storage.put_memory(
            memory_id=str(payload.get("id") or "") or None,
            scope_type=scope_type,
            scope_id=scope_id,
            category=str(payload.get("category", "status") or "status"),
            content=content,
            embedding=embedding,
            enabled=bool(payload.get("enabled", True)),
            source_session_id=str(payload.get("source_session_id", "")),
            source_turn=int(payload.get("source_turn", 0) or 0),
        )
        return self.ok({"id": memory_id, "warning": warning})

    async def toggle_memory(self, memory_id: str):
        payload = await request.get_json(force=True)
        enabled = bool((payload or {}).get("enabled", True))
        return self.ok({"updated": self.storage.set_memory_enabled(memory_id, enabled)})

    async def delete_memory(self, memory_id: str):
        return self.ok({"deleted": self.storage.delete_memory(memory_id)})

    async def metrics(self):
        days = float(request.args.get("days", 7) or 7)
        since = time.time() - max(0.01, days) * 86400
        rows = self.storage.list_metrics(
            session_id=request.args.get("session_id"),
            provider_id=request.args.get("provider_id"),
            since=since,
            limit=int(request.args.get("limit", 1000)),
        )
        rows = list(reversed(rows))
        providers: dict[str, int] = {}
        totals = {
            "requests": len(rows),
            "prompt_tokens": 0,
            "duration_ms": 0,
            "worldbook_hits": 0,
            "summary_generated": 0,
            "summary_failed": 0,
            "memory_hits": 0,
            "warnings": 0,
        }
        for row in rows:
            provider_id = str(row.get("provider_id") or "unknown")
            providers[provider_id] = providers.get(provider_id, 0) + 1
            totals["prompt_tokens"] += int(row.get("prompt_tokens", 0) or 0)
            totals["duration_ms"] += int(row.get("duration_ms", 0) or 0)
            totals["worldbook_hits"] += int(row.get("worldbook_hits", 0) or 0)
            totals["summary_generated"] += int(row.get("summary_generated", 0) or 0)
            totals["summary_failed"] += int(row.get("summary_failed", 0) or 0)
            totals["memory_hits"] += int(row.get("memory_hits", 0) or 0)
            totals["warnings"] += int(row.get("warning_count", 0) or 0)
        totals["avg_duration_ms"] = int(totals["duration_ms"] / len(rows)) if rows else 0
        return self.ok({"items": rows, "totals": totals, "providers": providers})

    async def retrieval_test(self):
        payload = await request.get_json(force=True)
        payload = payload if isinstance(payload, dict) else {}
        text = str(payload.get("text", "")).strip()
        if not text:
            return self.error("请填写检索测试文本")
        scopes = [("global", "*")]
        for scope_type, key in (("session", "session_id"), ("persona", "persona_id"),
                                ("user", "user_id"), ("group", "group_id")):
            value = str(payload.get(key, "") or "")
            if value:
                scopes.append((scope_type, value))
        entries = await self.service._collect_entries(scopes)
        scores = await self.service._hybrid_matcher(text, entries)
        entries_by_uid = {entry.uid: entry for entry in entries}
        matches = []
        for uid, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            entry = entries_by_uid.get(uid)
            if not entry:
                continue
            ext = entry.raw.get("extensions") if isinstance(entry.raw.get("extensions"), dict) else {}
            matches.append({
                "uid": uid,
                "name": entry.comment,
                "score": score,
                "category": str(entry.raw.get("category", ext.get("category", entry.group or "")) or ""),
                "keywords": entry.keys[:8],
                "content": entry.content[:240],
                "document_id": str(entry.raw.get("document_id", "")),
                "document_name": str(entry.raw.get("document_name", "")),
                "kind": str(entry.raw.get("kind", "")),
            })
        return self.ok({
            "query": text,
            "mode": str(self.service.config.get("retrieval_mode", "hybrid") or "hybrid"),
            "fts_available": bool(self.storage.fts_available),
            "candidate_count": int(self.service.config.get("retrieval_candidate_k", 60)),
            "top_k": int(self.service.config.get("retrieval_top_k", 8)),
            "matches": matches,
        })

    async def retrieval_stats(self):
        session_id = str(request.args.get("session_id", "") or "")
        return self.ok(await self.service.get_retrieval_stats(session_id))

    async def preview(self, session_id: str):
        payload = self.storage.get_preview(session_id)
        return self.ok(payload) if payload else self.error("Preview not found", 404)

    async def session(self, session_id: str):
        return self.ok(self.storage.get_session(session_id))

    async def reset_session(self, session_id: str):
        await self.service.reset_session(session_id)
        return self.ok()

    async def backup_session(self, session_id: str):
        content = build_session_backup(
            session_id,
            self.storage.get_session(session_id),
            self.storage.get_preview(session_id),
            [
                self.storage.get_story_node(str(item.get("id", ""))) or item
                for item in self.storage.list_story_nodes(session_id=session_id, limit=1000)
            ],
        )
        filename = f"tavern-session-{safe_filename(session_id, 'session')}.zip"
        return self.ok(download_payload(filename, "application/zip", content))

    async def archive_nodes(self):
        session_id = str(request.args.get("session_id", "") or "")
        limit = min(1000, max(1, int(request.args.get("limit", 300))))
        return self.ok(self.storage.list_story_nodes(session_id=session_id or None, limit=limit))

    async def archive_node(self, node_id: str):
        node = self.storage.get_story_node(node_id)
        return self.ok(node) if node else self.error("Story node not found", 404)

    async def archive_rename(self, node_id: str):
        payload = await request.get_json(force=True)
        ok = self.storage.rename_story_node(
            node_id,
            title=str(payload.get("title", "")) if "title" in payload else None,
            branch_name=str(payload.get("branch_name", "")) if "branch_name" in payload else None,
        )
        return self.ok() if ok else self.error("Story node not found", 404)

    async def archive_branch(self, node_id: str):
        payload = await request.get_json(force=True)
        session_id = str(payload.get("session_id", "") or "")
        if not session_id:
            node = self.storage.get_story_node(node_id)
            session_id = str((node or {}).get("session_id", "") or "")
        if not session_id:
            return self.error("Missing session_id")
        ok = await self.service.set_pending_branch(session_id, node_id, str(payload.get("branch_name", "") or ""))
        return self.ok() if ok else self.error("Story node not found", 404)

    async def archive_export(self):
        payload = await request.get_json(force=True)
        session_id = str(payload.get("session_id", "") or "")
        nodes = [
            self.storage.get_story_node(str(item.get("id", ""))) or item
            for item in self.storage.list_story_nodes(session_id=session_id or None, limit=1000)
        ]
        content = json.dumps(nodes, ensure_ascii=False, indent=2).encode("utf-8")
        name = safe_filename(session_id or "archive", "archive")
        return self.ok(download_payload(f"tavern-archive-{name}.json", "application/json", content))

    async def generation(self):
        payload = await request.get_json(force=True)
        session_id = str(payload.get("session_id", ""))
        mode = str(payload.get("mode", "normal"))
        if not session_id or mode not in {"normal", "continue", "impersonate", "quiet"}:
            return self.error("Invalid session_id or mode")
        await self.service.set_pending_generation(
            session_id,
            mode,
            str(payload.get("prompt", "")),
        )
        return self.ok()

    async def overview(self):
        counts = self.storage.document_counts()
        bindings = self.storage.list_bindings()
        tasks = []
        if not counts.get("character"):
            tasks.append("创建或导入一张角色卡")
        if not any(item["kind"] == "character" for item in bindings):
            tasks.append("把角色绑定到 Persona 或会话")
        if counts.get("lorebook") and not any(item["kind"] == "lorebook" for item in bindings):
            tasks.append("已有世界书尚未绑定")
        return self.ok({"version": PLUGIN_VERSION, "counts": counts, "bindings": len(bindings),
                        "tasks": tasks, "ready": not tasks})

    async def personas(self):
        try:
            values = await self.context.persona_manager.get_all_personas()
            items = [{"id": item.persona_id, "name": item.persona_id,
                      "prompt": item.system_prompt, "folder_id": item.folder_id}
                     for item in values]
            return self.ok(items)
        except Exception as exc:
            return self.ok([], warning=f"读取 AstrBot Persona 失败：{exc}")

    def _merge_bound_conversations(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {str(item.get("id", "")): item for item in items if item.get("id")}
        for binding in self.storage.list_bindings(scope_type="session"):
            session_id = str(binding.get("scope_id", ""))
            if not session_id or session_id in by_id:
                continue
            parts = session_id.split(":", 2)
            platform = parts[0] if parts else ""
            message_type = parts[1] if len(parts) > 1 else "会话"
            target = parts[2] if len(parts) > 2 else session_id
            by_id[session_id] = {
                "id": session_id, "conversation_id": "",
                "title": f"已绑定会话 · {message_type} · {target}",
                "platform": platform, "persona_id": "", "updated_at": 0,
                "source": "binding",
            }
        return sorted(by_id.values(), key=lambda item: (item.get("source") != "binding", -int(item.get("updated_at", 0) or 0)))

    async def conversations(self):
        warnings: list[str] = []
        try:
            page = max(1, int(request.args.get("page", 1)))
            page_size = min(100, max(1, int(request.args.get("page_size", 50))))
            values, total = await self.context.conversation_manager.get_filtered_conversations(
                page=page, page_size=page_size, search_query=str(request.args.get("search", "")))
            items = [{"id": item.user_id, "conversation_id": item.cid,
                      "title": item.title or item.user_id, "platform": item.platform_id,
                      "persona_id": item.persona_id or "", "updated_at": item.updated_at}
                     for item in values]
            items = self._merge_bound_conversations(items)
            return self.ok({"items": items, "total": max(total, len(items)), "page": page,
                            "warnings": warnings})
        except Exception as exc:
            warnings.append(f"读取 AstrBot 会话目录失败，已显示插件绑定记录：{exc}")
            items = self._merge_bound_conversations([])
            return self.ok({"items": items, "total": len(items), "page": 1,
                            "warnings": warnings})

    async def simulate(self):
        payload = await request.get_json(force=True)
        try:
            return self.ok(await self.service.simulate(payload or {}))
        except Exception as exc:
            return self.error(f"模拟失败：{exc}")
