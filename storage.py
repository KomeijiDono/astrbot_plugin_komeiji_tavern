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
    SCHEMA_VERSION = 2

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
