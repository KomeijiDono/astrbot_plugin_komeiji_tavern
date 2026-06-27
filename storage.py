from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


class TavernStorage:
    SCHEMA_VERSION = 6

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.fts_available = False
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
        CREATE TABLE IF NOT EXISTS entry_index (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            entry_uid TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            keys_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            embedding TEXT NOT NULL DEFAULT '[]',
            embedding_model TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entry_index_document
        ON entry_index(document_id, kind);
        CREATE INDEX IF NOT EXISTS idx_entry_index_hash
        ON entry_index(content_hash);
        CREATE INDEX IF NOT EXISTS idx_entry_index_enabled
        ON entry_index(enabled, updated_at);
        """
        with self._connection() as conn:
            conn.executescript(schema)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
            self._initialize_fts(conn)
            self._migrate_schema(conn)

    def _initialize_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts USING fts5(
                entry_id UNINDEXED,
                document_id UNINDEXED,
                kind UNINDEXED,
                name,
                content,
                keys,
                tokenize='unicode61'
            )
            """)
            self.fts_available = True
        except sqlite3.DatabaseError:
            self.fts_available = False

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "entry_index", "category", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "entry_index", "description", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "entry_index", "aliases_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "entry_index", "secondary_keys_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column(conn, "entry_index", "priority", "INTEGER NOT NULL DEFAULT 100")
        self._ensure_column(conn, "entry_index", "constant", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "entry_index", "inject_position", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "entry_index", "match_count", "INTEGER NOT NULL DEFAULT 0")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_logs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT '',
            matched_entries TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        )
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_retrieval_logs_created_at
        ON retrieval_logs(created_at)
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_retrieval_logs_session
        ON retrieval_logs(session_id, created_at)
        """)

    def rebuild_document_index(
        self,
        document_id: str,
        kind: str,
        name: str,
        data: dict[str, Any],
    ) -> int:
        entries = data.get("entries")
        if not entries:
            return 0
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            return 0

        now = time.time()

        old_embeddings: dict[str, tuple[str, str]] = {}
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT content_hash, embedding, embedding_model FROM entry_index WHERE document_id=?",
                (document_id,),
            ).fetchall()
            for row in rows:
                content_hash = str(row["content_hash"])
                embedding = str(row["embedding"] or "[]")
                embedding_model = str(row["embedding_model"] or "")
                if content_hash and embedding != "[]":
                    old_embeddings[content_hash] = (embedding, embedding_model)

        result_rows: list[tuple[str, str, str, str, str, str, str, str, str, str, str, int, float, str, str, str, str, int, int, int, int]] = []
        fts_rows: list[tuple[str, str, str, str, str, str]] = []

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("uid", entry.get("id", f"entry_{index}")))
            content = str(entry.get("content", ""))
            if not content:
                continue

            entry_id = f"{document_id}:{uid}:{index}"
            name_val = str(entry.get("comment", "") or entry.get("title", "") or entry.get("name", ""))
            keys = entry.get("key", entry.get("keys", []))
            if isinstance(keys, str):
                keys = [keys]
            keys_secondary = entry.get("keysecondary", entry.get("secondary_keywords", []))
            if isinstance(keys_secondary, str):
                keys_secondary = [keys_secondary]
            aliases = entry.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            all_keys = keys + keys_secondary + aliases
            keys_json = json.dumps(keys, ensure_ascii=False)
            secondary_keys_json = json.dumps(keys_secondary, ensure_ascii=False)
            aliases_json = json.dumps(aliases, ensure_ascii=False)
            content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()

            ext = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
            category = str(entry.get("category", ext.get("category", entry.get("group", ""))) or "")
            description = str(entry.get("description", ext.get("description", "")) or "")
            priority = int(entry.get("priority", ext.get("priority", entry.get("order", 100))) or 100)
            constant = bool(entry.get("constant", entry.get("is_constant", False)))
            inject_position = int(entry.get("inject_position", ext.get("inject_position", 2)) or 2)
            enabled = 0 if bool(entry.get("disable", entry.get("disabled", False))) else 1

            if content_hash in old_embeddings:
                embedding_json, embedding_model = old_embeddings[content_hash]
            else:
                embedding_json = "[]"
                embedding_model = ""

            metadata = {
                "position": entry.get("position"),
                "depth": entry.get("depth"),
                "role": entry.get("role"),
                "order": entry.get("order"),
                "vectorized": entry.get("vectorized", False),
                "group": entry.get("group", ""),
                "source_document_name": name,
                "category": category,
                "description": description,
                "aliases": aliases,
                "priority": priority,
                "constant": constant,
                "inject_position": inject_position,
            }
            metadata_json = json.dumps(metadata, ensure_ascii=False)

            fts_text = " ".join(all_keys + [category, description, name_val])
            result_rows.append((
                entry_id, document_id, kind, str(uid), name_val, content,
                keys_json, metadata_json, embedding_json, embedding_model, content_hash, enabled, now,
                category, description, aliases_json, secondary_keys_json, priority, int(constant), inject_position, 0,
            ))
            fts_rows.append((entry_id, document_id, kind, name_val, content, fts_text))

        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM entry_index WHERE document_id=?", (document_id,))
            if self.fts_available:
                conn.execute("DELETE FROM entry_fts WHERE document_id=?", (document_id,))

            if result_rows:
                conn.executemany(
                    """INSERT OR REPLACE INTO entry_index(
                        id, document_id, kind, entry_uid, name, content,
                        keys_json, metadata_json, embedding, embedding_model,
                        content_hash, enabled, updated_at,
                        category, description, aliases_json, secondary_keys_json,
                        priority, constant, inject_position, match_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    result_rows,
                )

            if fts_rows and self.fts_available:
                conn.executemany(
                    """INSERT INTO entry_fts(entry_id, document_id, kind, name, content, keys)
                    VALUES(?,?,?,?,?,?)""",
                    fts_rows,
                )

        return len(result_rows)

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
        if kind in {"lorebook", "material"}:
            self.rebuild_document_index(document_id, kind, name, data)
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
            conn.execute("DELETE FROM entry_index WHERE document_id=?", (document_id,))
            if self.fts_available:
                conn.execute("DELETE FROM entry_fts WHERE document_id=?", (document_id,))
        return bool(result.rowcount)

    @staticmethod
    def _decode_document(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = json.loads(result["data"])
        result["raw"] = json.loads(result["raw"])
        return result

    def get_entry_embedding_by_hash(self, content_hash: str, embedding_model: str = "") -> list[float]:
        with self._connection() as conn:
            if embedding_model:
                row = conn.execute(
                    "SELECT embedding FROM entry_index WHERE content_hash=? AND embedding_model=? AND enabled=1",
                    (content_hash, embedding_model),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT embedding FROM entry_index WHERE content_hash=? AND enabled=1",
                    (content_hash,),
                ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["embedding"])
        except (json.JSONDecodeError, TypeError):
            return []

    def update_entry_embedding(
        self,
        content_hash: str,
        embedding: list[float],
        embedding_model: str = "",
    ) -> int:
        with self._lock, self._connection() as conn:
            result = conn.execute(
                """UPDATE entry_index SET embedding=?, embedding_model=?
                WHERE content_hash=?""",
                (json.dumps(embedding), embedding_model, content_hash),
            )
        return result.rowcount

    def list_index_entries_by_hashes(
        self,
        hashes: list[str],
    ) -> list[dict[str, Any]]:
        if not hashes:
            return []
        placeholders = ",".join("?" for _ in hashes)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM entry_index WHERE content_hash IN ({placeholders})",
                hashes,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _fts_query(text: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", text)
        return " OR ".join(tokens[:20])

    def search_entries_fts(
        self,
        query: str,
        *,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return []

        if self.fts_available:
            try:
                with self._connection() as conn:
                    rows = conn.execute(
                        """SELECT
                            f.entry_id,
                            f.document_id,
                            f.kind,
                            i.entry_uid,
                            i.name,
                            i.content,
                            i.content_hash,
                            i.keys_json,
                            i.metadata_json,
                            bm25(entry_fts) AS bm25_score
                        FROM entry_fts f
                        JOIN entry_index i ON i.id = f.entry_id
                        WHERE entry_fts MATCH ? AND i.enabled = 1
                        ORDER BY bm25_score
                        LIMIT ?""",
                        (fts_query, limit),
                    ).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    bm25_score = float(item.pop("bm25_score", 0.0))
                    item["score"] = 1.0 / (1.0 + max(0.0, bm25_score))
                    result.append(item)
                return result
            except sqlite3.DatabaseError:
                pass

        like_pattern = f"%{query}%"
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT id AS entry_id, document_id, kind, entry_uid, name, content,
                content_hash, keys_json, metadata_json
                FROM entry_index
                WHERE enabled=1
                  AND (content LIKE ? OR name LIKE ? OR keys_json LIKE ?)
                ORDER BY updated_at DESC
                LIMIT ?""",
                (like_pattern, like_pattern, like_pattern, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["score"] = 0.5
            result.append(item)
        return result

    def record_retrieval_log(
        self,
        *,
        session_id: str,
        query: str,
        mode: str,
        matches: list[dict[str, Any]],
    ) -> str:
        log_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO retrieval_logs(id, session_id, query, mode, matched_entries, created_at)
                VALUES(?,?,?,?,?,?)""",
                (log_id, session_id, query, mode, json.dumps(matches, ensure_ascii=False), now),
            )
        return log_id

    def increment_entry_match_counts(self, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        placeholders = ",".join("?" for _ in entry_ids)
        with self._lock, self._connection() as conn:
            result = conn.execute(
                f"UPDATE entry_index SET match_count = match_count + 1 WHERE id IN ({placeholders})",
                entry_ids,
            )
        return result.rowcount

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
