from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


class TavernStorage:
    SCHEMA_VERSION = 4

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        self._backup_legacy_database()
        schema = """
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            data TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind, name);
        CREATE TABLE IF NOT EXISTS bindings (
            scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
            kind TEXT NOT NULL, target_id TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(scope_type, scope_id, kind, target_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS previews (
            session_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            source_session_id TEXT NOT NULL DEFAULT '',
            source_turn INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id, enabled, updated_at);
        CREATE TABLE IF NOT EXISTS runtime_metrics (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            provider_id TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'normal',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            block_count INTEGER NOT NULL DEFAULT 0,
            worldbook_hits INTEGER NOT NULL DEFAULT 0,
            summary_generated INTEGER NOT NULL DEFAULT 0,
            summary_failed INTEGER NOT NULL DEFAULT 0,
            memory_hits INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runtime_metrics_created_at ON runtime_metrics(created_at);
        CREATE INDEX IF NOT EXISTS idx_runtime_metrics_session ON runtime_metrics(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);
        CREATE INDEX IF NOT EXISTS idx_previews_updated_at ON previews(updated_at);
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        """
        with self._connection() as conn:
            conn.executescript(schema)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )

    def _backup_legacy_database(self) -> None:
        if not self.path.exists():
            return
        backup = self.path.with_name(f"{self.path.name}.v0.1.0.bak")
        if backup.exists():
            return
        conn: sqlite3.Connection | None = None
        backup_conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path)
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if not has_meta:
                backup_conn = sqlite3.connect(backup)
                conn.backup(backup_conn)
        except sqlite3.DatabaseError:
            return
        finally:
            if conn is not None:
                conn.close()
            if backup_conn is not None:
                backup_conn.close()

    def put_document(
        self, kind: str, name: str, data: dict[str, Any], *,
        document_id: str | None = None, raw: dict[str, Any] | None = None,
    ) -> str:
        document_id = document_id or str(uuid.uuid4())
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO documents(id,kind,name,data,raw,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,name=excluded.name,data=excluded.data,
                raw=excluded.raw,updated_at=excluded.updated_at""",
                (document_id, kind, name, json.dumps(data, ensure_ascii=False),
                 json.dumps(raw or data, ensure_ascii=False), now, now),
            )
        return document_id

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        return self._decode_document(row) if row else None

    def list_documents(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM documents", ()
        if kind:
            sql, params = sql + " WHERE kind=?", (kind,)
        sql += " ORDER BY kind,name,id"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_document(row) for row in rows]

    def document_counts(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = conn.execute("SELECT kind,COUNT(*) AS count FROM documents GROUP BY kind").fetchall()
        return {str(row["kind"]): int(row["count"]) for row in rows}

    def duplicate_document(self, document_id: str, name: str | None = None) -> str | None:
        document = self.get_document(document_id)
        if not document:
            return None
        return self.put_document(
            document["kind"], name or f'{document["name"]}（副本）', document["data"], raw=document["raw"]
        )

    def delete_document(self, document_id: str) -> bool:
        with self._lock, self._connection() as conn:
            result = conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
            conn.execute("DELETE FROM bindings WHERE target_id=?", (document_id,))
        return bool(result.rowcount)

    @staticmethod
    def _decode_document(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = json.loads(result["data"])
        result["raw"] = json.loads(result["raw"])
        return result

    def bind(self, scope_type: str, scope_id: str, kind: str, target_id: str, priority: int = 0) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bindings(scope_type,scope_id,kind,target_id,priority) VALUES(?,?,?,?,?)",
                (scope_type, scope_id, kind, target_id, priority),
            )

    def list_bindings(
        self, *, scope_type: str | None = None, scope_id: str | None = None,
        kind: str | None = None, target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("b.scope_type", scope_type), ("b.scope_id", scope_id),
                              ("b.kind", kind), ("b.target_id", target_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT b.*,d.name AS target_name FROM bindings b "
                "LEFT JOIN documents d ON d.id=b.target_id" + where +
                " ORDER BY b.scope_type,b.scope_id,b.kind,b.priority,d.name", params,
            ).fetchall()
        return [dict(row) for row in rows]

    def unbind(self, scope_type: str, scope_id: str, kind: str, target_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "DELETE FROM bindings WHERE scope_type=? AND scope_id=? AND kind=? AND target_id=?",
                (scope_type, scope_id, kind, target_id),
            )

    def resolve_bindings(self, kind: str, scopes: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        pairs = list(scopes)
        if not pairs:
            return []
        clauses = " OR ".join("(b.scope_type=? AND b.scope_id=?)" for _ in pairs)
        params: list[Any] = [kind]
        for pair in pairs:
            params.extend(pair)
        sql = f"""SELECT d.*, MIN(b.priority) AS binding_priority FROM bindings b
        JOIN documents d ON d.id=b.target_id WHERE b.kind=? AND ({clauses})
        GROUP BY d.id ORDER BY binding_priority,d.name"""
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_document(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT state FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row["state"]) if row else {"turn": 0, "effects": {}, "variables": {}, "group_index": 0}

    def save_session(self, session_id: str, state: dict[str, Any]) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions(session_id,state,updated_at) VALUES(?,?,?)",
                (session_id, json.dumps(state, ensure_ascii=False), time.time()),
            )

    def reset_session(self, session_id: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM previews WHERE session_id=?", (session_id,))

    def save_preview(self, session_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO previews(session_id,payload,updated_at) VALUES(?,?,?)",
                (session_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )

    def get_preview(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM previews WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_memory(
        self,
        *,
        scope_type: str,
        scope_id: str,
        category: str,
        content: str,
        embedding: list[float] | None = None,
        memory_id: str | None = None,
        enabled: bool = True,
        source_session_id: str = "",
        source_turn: int = 0,
    ) -> str:
        memory_id = memory_id or str(uuid.uuid4())
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO memories(
                    id,scope_type,scope_id,category,content,embedding,enabled,
                    source_session_id,source_turn,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    scope_type=excluded.scope_type,scope_id=excluded.scope_id,
                    category=excluded.category,content=excluded.content,
                    embedding=excluded.embedding,enabled=excluded.enabled,
                    source_session_id=excluded.source_session_id,
                    source_turn=excluded.source_turn,updated_at=excluded.updated_at""",
                (
                    memory_id,
                    scope_type,
                    scope_id,
                    category,
                    content,
                    json.dumps(embedding or []),
                    1 if enabled else 0,
                    source_session_id,
                    int(source_turn or 0),
                    now,
                    now,
                ),
            )
        return memory_id

    def list_memories(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        enabled: bool | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_type:
            clauses.append("scope_type=?")
            params.append(scope_type)
        if scope_id:
            clauses.append("scope_id=?")
            params.append(scope_id)
        if enabled is not None:
            clauses.append("enabled=?")
            params.append(1 if enabled else 0)
        if query:
            clauses.append("(content LIKE ? OR category LIKE ? OR scope_id LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memories" + where + " ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_memory(row) for row in rows]

    def set_memory_enabled(self, memory_id: str, enabled: bool) -> bool:
        with self._lock, self._connection() as conn:
            result = conn.execute(
                "UPDATE memories SET enabled=?,updated_at=? WHERE id=?",
                (1 if enabled else 0, time.time(), memory_id),
            )
        return bool(result.rowcount)

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock, self._connection() as conn:
            result = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return bool(result.rowcount)

    @staticmethod
    def _decode_memory(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["embedding"] = json.loads(result.get("embedding") or "[]")
        except json.JSONDecodeError:
            result["embedding"] = []
        result["enabled"] = bool(result.get("enabled"))
        return result

    def record_metric(self, payload: dict[str, Any]) -> str:
        metric_id = str(payload.get("id") or uuid.uuid4())
        created_at = float(payload.get("created_at") or time.time())
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO runtime_metrics(
                    id,session_id,provider_id,mode,prompt_tokens,message_count,
                    block_count,worldbook_hits,summary_generated,summary_failed,
                    memory_hits,warning_count,duration_ms,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    metric_id,
                    str(payload.get("session_id", "")),
                    str(payload.get("provider_id", "")),
                    str(payload.get("mode", "normal") or "normal"),
                    int(payload.get("prompt_tokens", 0) or 0),
                    int(payload.get("message_count", 0) or 0),
                    int(payload.get("block_count", 0) or 0),
                    int(payload.get("worldbook_hits", 0) or 0),
                    1 if payload.get("summary_generated") else 0,
                    1 if payload.get("summary_failed") else 0,
                    int(payload.get("memory_hits", 0) or 0),
                    int(payload.get("warning_count", 0) or 0),
                    int(payload.get("duration_ms", 0) or 0),
                    created_at,
                ),
            )
        return metric_id

    def list_metrics(
        self,
        *,
        session_id: str | None = None,
        provider_id: str | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if provider_id:
            clauses.append("provider_id=?")
            params.append(provider_id)
        if since is not None:
            clauses.append("created_at>=?")
            params.append(float(since))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM runtime_metrics" + where + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_expired(
        self,
        *,
        session_cutoff: float | None = None,
        preview_cutoff: float | None = None,
    ) -> dict[str, int]:
        deleted = {"sessions": 0, "previews": 0}
        with self._lock, self._connection() as conn:
            if session_cutoff is not None:
                result = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (session_cutoff,))
                deleted["sessions"] = int(result.rowcount)
            if preview_cutoff is not None:
                result = conn.execute("DELETE FROM previews WHERE updated_at < ?", (preview_cutoff,))
                deleted["previews"] = int(result.rowcount)
        return deleted

    def cleanup_expired_extended(
        self,
        *,
        session_cutoff: float | None = None,
        preview_cutoff: float | None = None,
        metric_cutoff: float | None = None,
        memory_cutoff: float | None = None,
    ) -> dict[str, int]:
        deleted = self.cleanup_expired(session_cutoff=session_cutoff, preview_cutoff=preview_cutoff)
        deleted.update({"metrics": 0, "memories": 0})
        with self._lock, self._connection() as conn:
            if metric_cutoff is not None:
                result = conn.execute("DELETE FROM runtime_metrics WHERE created_at < ?", (metric_cutoff,))
                deleted["metrics"] = int(result.rowcount)
            if memory_cutoff is not None:
                result = conn.execute("DELETE FROM memories WHERE updated_at < ?", (memory_cutoff,))
                deleted["memories"] = int(result.rowcount)
        return deleted
