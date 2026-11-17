"""SQLite WAL + FTS5 + vector blob + ingest cursor persistence layer."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from omnimex.core.models import (
    DistillStatus,
    IngestCursor,
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
    utc_now,
)
from omnimex.core.redaction import redact_memory_card, redact_session, redact_turn
from omnimex.storage.embeddings import (
    EmbeddingProvider,
    pack_embedding,
    unpack_embedding,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    workspace TEXT,
    workspace_norm TEXT,
    source_path TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    distill_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace_norm ON sessions(workspace_norm);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_distill ON sessions(distill_status);

CREATE TABLE IF NOT EXISTS turns (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    thinking TEXT,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    search_text TEXT NOT NULL DEFAULT '',
    timestamp TEXT,
    model TEXT,
    token_count INTEGER,
    redacted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    search_text,
    content='turns',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;

CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO turns_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;

CREATE TABLE IF NOT EXISTS turn_embeddings (
    turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    norm REAL NOT NULL DEFAULT 1.0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_embeddings_session ON turn_embeddings(session_id);
CREATE INDEX IF NOT EXISTS idx_turn_embeddings_model ON turn_embeddings(model, dimension);

CREATE TABLE IF NOT EXISTS ingest_cursors (
    source_path TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    line_number INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    checksum TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_cursors_agent ON ingest_cursors(agent);

CREATE TABLE IF NOT EXISTS memories (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    file_path TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    workspace TEXT,
    workspace_norm TEXT,
    distill_status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'manual',
    reviewed INTEGER NOT NULL DEFAULT 0,
    source_session_id TEXT,
    source_agent TEXT,
    source_turn_index INTEGER,
    native_source_path TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(distill_status);
CREATE INDEX IF NOT EXISTS idx_memories_origin ON memories(origin);
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(source_session_id);
CREATE INDEX IF NOT EXISTS idx_memories_workspace_norm ON memories(workspace_norm);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title,
    content,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memories_fts(rowid, title, content) VALUES (new.rowid, new.title, new.content);
END;
"""


def format_iso_utc(dt: datetime | str | None) -> str | None:
    """Format a datetime or ISO string into canonical UTC ISO-8601 ('...Z')."""
    if dt is None:
        return None
    if isinstance(dt, str):
        text = dt.strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        parsed = dt
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def normalize_date_bound(value: datetime | str | None, *, end_of_day: bool = False) -> str | None:
    """Normalize a date/timestamp query bound for lexicographical SQL comparisons."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-":
            suffix = "T23:59:59.999999Z" if end_of_day else "T00:00:00Z"
            return f"{stripped}{suffix}"
    return format_iso_utc(value)


def normalize_cursor_key(source_path: str | Path) -> str:
    """Normalize source path key for ingest_cursors consistency across slashes."""
    return str(source_path).replace("\\", "/")


def escape_like(value: str, escape_char: str = "!") -> str:
    """Escape LIKE wildcards safely."""
    return (
        value.replace(escape_char, escape_char + escape_char)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


def append_workspace_filter(
    clauses: list[str],
    params: list[Any],
    column: str,
    workspace: str | None,
) -> None:
    """Append a normalized exact-or-subpath workspace SQL filter."""
    norm = normalize_workspace_path(workspace)
    if not norm:
        return
    ws_lower = norm.lower()
    escaped = escape_like(ws_lower)
    prefix = escaped.rstrip("/") + "/%"
    clauses.append(f"({column} = ? OR {column} LIKE ? ESCAPE '!')")
    params.extend([ws_lower, prefix])


def build_turn_search_text(turn: NormalizedTurn) -> str:
    """Assemble searchable plain text for FTS5 & vector indexing from a turn."""
    parts: list[str] = []
    if turn.content:
        parts.append(turn.content)
    if turn.thinking:
        parts.append(turn.thinking)
    for tc in turn.tool_calls:
        arg_str = (
            json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)
            if isinstance(tc.arguments, dict)
            else str(tc.arguments)
        )
        tc_line = f"[tool:{tc.name}] {arg_str}".strip()
        if tc.output:
            output_excerpt = tc.output[:4000]
            tc_line = f"{tc_line}\n{output_excerpt}"
        parts.append(tc_line)
    return "\n".join(parts).strip()


def _workspace_norm(workspace: str | Path | None) -> str | None:
    norm = normalize_workspace_path(workspace)
    return norm.lower() if norm is not None else None


class OmnimexDB:
    """Thread-safe SQLite repository with WAL, FTS5, embeddings, cursors, and memories."""

    def __init__(
        self,
        db_path: str | Path = "data/omnimex.db",
        *,
        wal_mode: bool = True,
        busy_timeout_ms: int = 5000,
        redact: bool = True,
    ) -> None:
        self._raw_path = str(db_path)
        self._is_memory = self._raw_path == ":memory:"
        self.db_path = Path(db_path) if not self._is_memory else Path(":memory:")
        self._wal_mode = wal_mode
        self._busy_timeout_ms = busy_timeout_ms
        self._redact = redact
        self._lock = threading.RLock()
        self._closed = False
        self._thread_local = threading.local()
        self._read_conns: set[sqlite3.Connection] = set()

        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._raw_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self.init_schema()

    def _configure_pragmas(self) -> None:
        with self._lock:
            self._conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)};")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            if self._wal_mode and not self._is_memory:
                self._conn.execute("PRAGMA journal_mode = WAL;")

    def init_schema(self) -> None:
        """Create tables, FTS5 virtual tables, indexes, and sync triggers."""
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def journal_mode(self) -> str:
        """Return the active SQLite journal_mode (e.g. 'wal' or 'memory')."""
        with self._lock:
            row = self._conn.execute("PRAGMA journal_mode;").fetchone()
            return str(row[0]).lower() if row else ""

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a concurrent read connection (WAL RO per-thread for file DBs, locked for :memory:)."""
        if self._is_memory:
            with self._lock:
                yield self._conn
            return

        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            with self._lock:
                if self._closed:
                    raise sqlite3.ProgrammingError("Cannot operate on a closed OmnimexDB")
                uri = self.db_path.resolve().as_uri() + "?mode=ro"
                try:
                    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                except sqlite3.Error:
                    conn = sqlite3.connect(self._raw_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)};")
                conn.execute("PRAGMA query_only = ON;")
                self._thread_local.conn = conn
                self._read_conns.add(conn)
        yield conn

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for rconn in list(self._read_conns):
                with suppress(sqlite3.Error):
                    rconn.close()
            self._read_conns.clear()
            self._thread_local = threading.local()
            self._conn.close()

    def __enter__(self) -> OmnimexDB:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Sessions & Turns
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        session: NormalizedSession,
        *,
        replace_turns: bool = False,
    ) -> NormalizedSession:
        """Insert or update a session and its turns (scrubbing secrets when enabled)."""
        clean_session = redact_session(session) if self._redact else session
        now_iso = format_iso_utc(utc_now())
        assert now_iso is not None

        ws_norm = _workspace_norm(clean_session.workspace)
        started_iso = format_iso_utc(clean_session.started_at)
        updated_iso = format_iso_utc(clean_session.updated_at) or now_iso

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, agent, external_id, title, workspace, workspace_norm,
                    source_path, started_at, updated_at, turn_count, model,
                    tags_json, summary, distill_status, created_at, ingested_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    agent = excluded.agent,
                    external_id = excluded.external_id,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END,
                    workspace = COALESCE(excluded.workspace, sessions.workspace),
                    workspace_norm = COALESCE(excluded.workspace_norm, sessions.workspace_norm),
                    source_path = excluded.source_path,
                    started_at = COALESCE(sessions.started_at, excluded.started_at),
                    updated_at = COALESCE(excluded.updated_at, sessions.updated_at),
                    turn_count = MAX(sessions.turn_count, excluded.turn_count),
                    model = COALESCE(excluded.model, sessions.model),
                    tags_json = excluded.tags_json,
                    summary = COALESCE(excluded.summary, sessions.summary),
                    ingested_at = excluded.ingested_at
                """,
                (
                    clean_session.id,
                    clean_session.agent,
                    clean_session.external_id,
                    clean_session.title,
                    clean_session.workspace,
                    ws_norm,
                    clean_session.source_path,
                    started_iso,
                    updated_iso,
                    max(clean_session.turn_count, len(clean_session.turns)),
                    clean_session.model,
                    json.dumps(clean_session.tags, ensure_ascii=False),
                    clean_session.summary,
                    clean_session.distill_status,
                    started_iso or now_iso,
                    now_iso,
                ),
            )

            if replace_turns:
                self._conn.execute(
                    "DELETE FROM turns WHERE session_id = ?",
                    (clean_session.id,),
                )

            if clean_session.turns:
                self._upsert_turns_locked(clean_session.id, clean_session.turns)

            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM turns WHERE session_id = ?",
                (clean_session.id,),
            ).fetchone()
            actual_count = int(row["cnt"]) if row else 0
            effective_count = (
                actual_count if replace_turns else max(actual_count, clean_session.turn_count)
            )
            self._conn.execute(
                "UPDATE sessions SET turn_count = ? WHERE id = ?",
                (effective_count, clean_session.id),
            )

        return clean_session.model_copy(update={"turn_count": effective_count})

    def _upsert_turns_locked(
        self,
        session_id: str,
        turns: Sequence[NormalizedTurn],
    ) -> list[NormalizedTurn]:
        persisted: list[NormalizedTurn] = []
        for turn in turns:
            t = turn
            if not t.session_id:
                t = t.model_copy(update={"session_id": session_id})
            if not t.id:
                t = t.model_copy(update={"id": f"{session_id}#t{t.turn_index}"})

            tool_calls_payload = json.dumps(
                [tc.model_dump(mode="json") for tc in t.tool_calls],
                ensure_ascii=False,
            )
            search_text = build_turn_search_text(t)
            ts_iso = format_iso_utc(t.timestamp)

            self._conn.execute(
                """
                INSERT INTO turns (
                    id, session_id, turn_index, role, content, thinking,
                    tool_calls_json, search_text, timestamp, model, token_count, redacted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, turn_index) DO UPDATE SET
                    id = excluded.id,
                    role = excluded.role,
                    content = excluded.content,
                    thinking = excluded.thinking,
                    tool_calls_json = excluded.tool_calls_json,
                    search_text = excluded.search_text,
                    timestamp = COALESCE(excluded.timestamp, turns.timestamp),
                    model = COALESCE(excluded.model, turns.model),
                    token_count = COALESCE(excluded.token_count, turns.token_count),
                    redacted = MAX(turns.redacted, excluded.redacted)
                """,
                (
                    t.id,
                    session_id,
                    t.turn_index,
                    t.role,
                    t.content,
                    t.thinking,
                    tool_calls_payload,
                    search_text,
                    ts_iso,
                    t.model,
                    t.token_count,
                    1 if t.redacted else 0,
                ),
            )
            persisted.append(t)
        return persisted

    def upsert_turns(
        self,
        session_id: str,
        turns: Sequence[NormalizedTurn],
    ) -> list[NormalizedTurn]:
        """Insert or update turns for an existing session."""
        clean_turns = [redact_turn(t) if self._redact else t for t in turns]
        if not clean_turns:
            return []

        now_iso = format_iso_utc(utc_now())
        with self._lock, self._conn:
            persisted = self._upsert_turns_locked(session_id, clean_turns)
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt, MAX(timestamp) AS max_ts FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            cnt = int(row["cnt"]) if row else len(persisted)
            max_ts = row["max_ts"] if row and row["max_ts"] else now_iso
            self._conn.execute(
                """
                UPDATE sessions
                SET turn_count = MAX(turn_count, ?),
                    updated_at = COALESCE(?, updated_at),
                    ingested_at = ?
                WHERE id = ?
                """,
                (cnt, max_ts, now_iso, session_id),
            )
        return persisted

    def get_session(self, session_id: str, *, include_turns: bool = True) -> NormalizedSession | None:
        """Fetch a single session by ID, optionally including ordered turns."""
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            turns: list[NormalizedTurn] = []
            if include_turns:
                turn_rows = conn.execute(
                    "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_index ASC",
                    (session_id,),
                ).fetchall()
                turns = [self._row_to_turn(r) for r in turn_rows]
            return self._row_to_session(row, turns=turns)

    def get_session_turns_window(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 40,
        tail: bool = True,
        qa_only: bool = False,
    ) -> tuple[list[NormalizedTurn], int, int]:
        """Fetch a paginated turn window ``(turns, total_turns, effective_offset)``."""
        safe_limit = max(1, min(int(limit), 500))
        where_sql = (
            "session_id = ? AND ("
            "role = 'user' OR ("
            "role = 'assistant' "
            "AND TRIM(COALESCE(content, '')) != '' "
            "AND TRIM(content) NOT LIKE '[Tool:%'"
            ")"
            ")"
            if qa_only
            else "session_id = ?"
        )
        with self.read_connection() as conn:
            cnt_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM turns WHERE {where_sql}",
                (session_id,),
            ).fetchone()
            total_turns = int(cnt_row["cnt"]) if cnt_row else 0
            if total_turns == 0:
                return [], 0, 0

            if offset is None:
                effective_offset = max(0, total_turns - safe_limit) if tail else 0
            else:
                effective_offset = max(0, min(int(offset), total_turns))

            turn_rows = conn.execute(
                f"""
                SELECT * FROM turns
                WHERE {where_sql}
                ORDER BY turn_index ASC
                LIMIT ? OFFSET ?
                """,
                (session_id, safe_limit, effective_offset),
            ).fetchall()
            return [self._row_to_turn(r) for r in turn_rows], total_turns, effective_offset

    def get_session_turn(self, session_id: str, turn_index: int) -> NormalizedTurn | None:
        """Fetch a single turn by session ID and ``turn_index``."""
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? AND turn_index = ?",
                (session_id, int(turn_index)),
            ).fetchone()
            return self._row_to_turn(row) if row is not None else None

    def list_sessions(
        self,
        *,
        agents: Sequence[str] | None = None,
        workspace: str | None = None,
        query: str | None = None,
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        distill_status: DistillStatus | None = None,
        include_subagents: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[NormalizedSession], int]:
        """List sessions with filters, returning (sessions, total_matching)."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if agents:
            placeholders = ",".join("?" for _ in agents)
            where_clauses.append(f"agent IN ({placeholders})")
            params.extend(agents)

        append_workspace_filter(where_clauses, params, "workspace_norm", workspace)

        if query and query.strip():
            like_pattern = f"%{escape_like(query.strip().lower())}%"
            where_clauses.append(
                "(LOWER(title) LIKE ? ESCAPE '!' OR LOWER(COALESCE(summary, '')) LIKE ? ESCAPE '!' "
                "OR LOWER(tags_json) LIKE ? ESCAPE '!')"
            )
            params.extend([like_pattern, like_pattern, like_pattern])

        if bound_from := normalize_date_bound(date_from, end_of_day=False):
            where_clauses.append("updated_at >= ?")
            params.append(bound_from)

        if bound_to := normalize_date_bound(date_to, end_of_day=True):
            where_clauses.append("updated_at <= ?")
            params.append(bound_to)

        if distill_status is not None:
            where_clauses.append("distill_status = ?")
            params.append(distill_status)

        if not include_subagents:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM turns t WHERE t.session_id = sessions.id AND t.role = 'user')"
            )

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self.read_connection() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM sessions {where_sql}",
                params,
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

            rows = conn.execute(
                f"""
                SELECT * FROM sessions
                {where_sql}
                ORDER BY COALESCE(updated_at, started_at, created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return [self._row_to_session(r, turns=[]) for r in rows], total

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (cascading to turns, FTS5, and embeddings)."""
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    def mark_session_distill_status(self, session_id: str, status: DistillStatus) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET distill_status = ? WHERE id = ?",
                (status, session_id),
            )

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> NormalizedTurn:
        raw_tools = json.loads(row["tool_calls_json"] or "[]")
        tool_calls = [ToolCallRecord.model_validate(item) for item in raw_tools]
        return NormalizedTurn(
            id=row["id"],
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            role=row["role"],
            content=row["content"],
            thinking=row["thinking"],
            tool_calls=tool_calls,
            timestamp=row["timestamp"],
            model=row["model"],
            token_count=row["token_count"],
            redacted=bool(row["redacted"]),
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row, *, turns: list[NormalizedTurn]) -> NormalizedSession:
        tags = json.loads(row["tags_json"] or "[]")
        return NormalizedSession(
            id=row["id"],
            agent=row["agent"],
            external_id=row["external_id"],
            title=row["title"],
            workspace=row["workspace"],
            source_path=row["source_path"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            turn_count=row["turn_count"],
            model=row["model"],
            tags=tags,
            summary=row["summary"],
            turns=turns,
            distill_status=row["distill_status"],
        )

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def upsert_turn_embedding(
        self,
        turn_id: str,
        session_id: str,
        model: str,
        embedding: Sequence[float] | np.ndarray,
    ) -> None:
        self.upsert_turn_embeddings([(turn_id, session_id, model, embedding)])

    def upsert_turn_embeddings(
        self,
        items: Sequence[tuple[str, str, str, Sequence[float] | np.ndarray]],
    ) -> int:
        if not items:
            return 0
        now_iso = format_iso_utc(utc_now())
        assert now_iso is not None

        rows: list[tuple[str, str, str, int, bytes, float, str]] = []
        for turn_id, session_id, model, vec in items:
            arr = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            rows.append(
                (
                    turn_id,
                    session_id,
                    model,
                    int(arr.shape[0]),
                    pack_embedding(arr),
                    norm,
                    now_iso,
                )
            )

        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO turn_embeddings (
                    turn_id, session_id, model, dimension, embedding, norm, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    model = excluded.model,
                    dimension = excluded.dimension,
                    embedding = excluded.embedding,
                    norm = excluded.norm,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def index_session_embeddings(
        self,
        session_id: str,
        embedder: EmbeddingProvider,
        *,
        force: bool = False,
        batch_size: int = 32,
    ) -> int:
        """Compute and persist vector embeddings for turns in ``session_id``."""
        with self._lock:
            if force:
                rows = self._conn.execute(
                    """
                    SELECT id, search_text, content FROM turns
                    WHERE session_id = ?
                    ORDER BY turn_index ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT t.id, t.search_text, t.content
                    FROM turns t
                    LEFT JOIN turn_embeddings te
                        ON te.turn_id = t.id AND te.model = ? AND te.dimension = ?
                    WHERE t.session_id = ? AND te.turn_id IS NULL
                    ORDER BY t.turn_index ASC
                    """,
                    (embedder.model_name, embedder.dimension, session_id),
                ).fetchall()

        if not rows:
            return 0

        indexed = 0
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            texts = [str(r["search_text"] or r["content"] or "") for r in chunk]
            vectors = embedder.embed_texts(texts)
            batch_items = [
                (str(r["id"]), session_id, embedder.model_name, vec)
                for r, vec in zip(chunk, vectors, strict=True)
            ]
            indexed += self.upsert_turn_embeddings(batch_items)
        return indexed

    def get_turn_embedding(self, turn_id: str) -> tuple[str, np.ndarray] | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT model, embedding FROM turn_embeddings WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                return None
            return str(row["model"]), unpack_embedding(bytes(row["embedding"])).copy()

    def count_turn_embeddings(self, model: str | None = None) -> int:
        with self.read_connection() as conn:
            if model is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM turn_embeddings WHERE model = ?",
                    (model,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM turn_embeddings").fetchone()
            return int(row["cnt"]) if row else 0

    # ------------------------------------------------------------------
    # Ingest Cursors
    # ------------------------------------------------------------------

    def get_cursor(self, source_path: str | Path) -> IngestCursor | None:
        key = normalize_cursor_key(source_path)
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_cursors WHERE source_path = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return IngestCursor(
                source_path=row["source_path"],
                agent=row["agent"],
                byte_offset=row["byte_offset"],
                line_number=row["line_number"],
                mtime_ns=row["mtime_ns"],
                file_size=row["file_size"],
                checksum=row["checksum"],
                updated_at=row["updated_at"],
            )

    def upsert_cursor(self, cursor: IngestCursor) -> IngestCursor:
        key = normalize_cursor_key(cursor.source_path)
        now_iso = format_iso_utc(cursor.updated_at or utc_now())
        assert now_iso is not None
        normalized = cursor.model_copy(update={"source_path": key})

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO ingest_cursors (
                    source_path, agent, byte_offset, line_number,
                    mtime_ns, file_size, checksum, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    agent = excluded.agent,
                    byte_offset = excluded.byte_offset,
                    line_number = excluded.line_number,
                    mtime_ns = excluded.mtime_ns,
                    file_size = excluded.file_size,
                    checksum = excluded.checksum,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    normalized.agent,
                    normalized.byte_offset,
                    normalized.line_number,
                    normalized.mtime_ns,
                    normalized.file_size,
                    normalized.checksum,
                    now_iso,
                ),
            )
        return normalized

    def delete_cursor(self, source_path: str | Path) -> bool:
        key = normalize_cursor_key(source_path)
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM ingest_cursors WHERE source_path = ?", (key,))
            return cur.rowcount > 0

    def list_cursors(self, agent: str | None = None) -> list[IngestCursor]:
        with self.read_connection() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT * FROM ingest_cursors WHERE agent = ? ORDER BY source_path ASC",
                    (agent,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ingest_cursors ORDER BY source_path ASC"
                ).fetchall()
            return [
                IngestCursor(
                    source_path=r["source_path"],
                    agent=r["agent"],
                    byte_offset=r["byte_offset"],
                    line_number=r["line_number"],
                    mtime_ns=r["mtime_ns"],
                    file_size=r["file_size"],
                    checksum=r["checksum"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Memories
    # ------------------------------------------------------------------

    def upsert_memory(self, card: MemoryCard) -> MemoryCard:
        clean = redact_memory_card(card) if self._redact else card
        ws_norm = _workspace_norm(clean.workspace)
        created_iso = format_iso_utc(clean.created_at) or format_iso_utc(utc_now())
        updated_iso = format_iso_utc(clean.updated_at) or created_iso
        assert created_iso is not None and updated_iso is not None

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO memories (
                    id, title, category, content, file_path, tags_json,
                    workspace, workspace_norm, distill_status, origin, reviewed,
                    source_session_id, source_agent, source_turn_index,
                    native_source_path, confidence, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    content = excluded.content,
                    file_path = COALESCE(excluded.file_path, memories.file_path),
                    tags_json = excluded.tags_json,
                    workspace = excluded.workspace,
                    workspace_norm = excluded.workspace_norm,
                    distill_status = excluded.distill_status,
                    origin = excluded.origin,
                    reviewed = excluded.reviewed,
                    source_session_id = COALESCE(excluded.source_session_id, memories.source_session_id),
                    source_agent = COALESCE(excluded.source_agent, memories.source_agent),
                    source_turn_index = COALESCE(excluded.source_turn_index, memories.source_turn_index),
                    native_source_path = COALESCE(excluded.native_source_path, memories.native_source_path),
                    confidence = COALESCE(excluded.confidence, memories.confidence),
                    updated_at = excluded.updated_at
                """,
                (
                    clean.id,
                    clean.title,
                    clean.category,
                    clean.content,
                    clean.file_path,
                    json.dumps(clean.tags, ensure_ascii=False),
                    clean.workspace,
                    ws_norm,
                    clean.distill_status,
                    clean.origin,
                    1 if clean.reviewed else 0,
                    clean.source_session_id,
                    clean.source_agent,
                    clean.source_turn_index,
                    clean.native_source_path,
                    clean.confidence,
                    created_iso,
                    updated_iso,
                ),
            )
        return clean

    def get_memory(self, memory_id: str) -> MemoryCard | None:
        with self.read_connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        *,
        category: str | None = None,
        distill_status: str | None = None,
        origin: str | None = None,
        reviewed: bool | None = None,
        workspace: str | None = None,
        agent: str | None = None,
        source_session_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryCard]:
        where_clauses: list[str] = []
        params: list[Any] = []

        if category:
            where_clauses.append("category = ?")
            params.append(category)
        if distill_status:
            where_clauses.append("distill_status = ?")
            params.append(distill_status)
        if origin:
            where_clauses.append("origin = ?")
            params.append(origin)
        if reviewed is not None:
            where_clauses.append("reviewed = ?")
            params.append(1 if reviewed else 0)
        append_workspace_filter(where_clauses, params, "workspace_norm", workspace)
        if agent:
            where_clauses.append("source_agent = ?")
            params.append(agent)
        if source_session_id:
            where_clauses.append("source_session_id = ?")
            params.append(source_session_id)
        if query and query.strip():
            like_pat = f"%{escape_like(query.strip().lower())}%"
            where_clauses.append(
                "(LOWER(title) LIKE ? ESCAPE '!' OR LOWER(content) LIKE ? ESCAPE '!' "
                "OR LOWER(tags_json) LIKE ? ESCAPE '!')"
            )
            params.extend([like_pat, like_pat, like_pat])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        with self.read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memories
                {where_sql}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return [self._row_to_memory(r) for r in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryCard:
        return MemoryCard(
            id=row["id"],
            title=row["title"],
            category=row["category"],
            content=row["content"],
            file_path=row["file_path"],
            tags=json.loads(row["tags_json"] or "[]"),
            workspace=row["workspace"],
            distill_status=row["distill_status"],
            origin=row["origin"],
            reviewed=bool(row["reviewed"]),
            source_session_id=row["source_session_id"],
            source_agent=row["source_agent"],
            source_turn_index=row["source_turn_index"],
            native_source_path=row["native_source_path"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Analytics & Health
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return aggregated counts for health/stats endpoints."""
        with self.read_connection() as conn:
            total_sessions = int(
                conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            )
            total_turns = int(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
            total_memories = int(
                conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            )
            total_native_memories = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE origin = 'native_import'"
                ).fetchone()[0]
            )
            embeddings_count = int(
                conn.execute("SELECT COUNT(*) FROM turn_embeddings").fetchone()[0]
            )
            pending_sessions_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE distill_status = 'pending'"
                ).fetchone()[0]
            )
            distilled_memories_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE origin = 'distilled'"
                ).fetchone()[0]
            )

            agent_rows = conn.execute(
                """
                SELECT agent,
                       COUNT(*) AS sessions,
                       COALESCE(SUM(turn_count), 0) AS turns,
                       MAX(updated_at) AS last_activity_at
                FROM sessions
                GROUP BY agent
                ORDER BY sessions DESC, agent ASC
                """
            ).fetchall()

            cat_counts = {"preference": 0, "decision": 0, "pitfall": 0, "fact": 0}
            for r in conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM memories GROUP BY category"
            ).fetchall():
                cat_counts[str(r["category"])] = int(r["cnt"])

            status_counts = {"heuristic": 0, "llm_distilled": 0, "pending": 0, "manual": 0}
            for r in conn.execute(
                """
                SELECT CASE
                           WHEN origin = 'manual' THEN 'manual'
                           ELSE distill_status
                       END AS status_bucket,
                       COUNT(*) AS cnt
                FROM memories
                GROUP BY status_bucket
                """
            ).fetchall():
                status_counts[str(r["status_bucket"])] = int(r["cnt"])

        db_size_bytes = 0
        if not self._is_memory and self.db_path.exists():
            db_size_bytes = self.db_path.stat().st_size

        return {
            "total_sessions": total_sessions,
            "total_turns": total_turns,
            "total_memories": total_memories,
            "total_native_memories": total_native_memories,
            "db_size_bytes": db_size_bytes,
            "index_embeddings_count": embeddings_count,
            "pending_sessions_count": pending_sessions_count,
            "distilled_memories_count": distilled_memories_count,
            "by_agent": [
                {
                    "agent": r["agent"],
                    "sessions": int(r["sessions"]),
                    "turns": int(r["turns"]),
                    "last_activity_at": r["last_activity_at"],
                }
                for r in agent_rows
            ],
            "memories_by_category": cat_counts,
            "memories_by_status": status_counts,
        }


__all__ = [
    "OmnimexDB",
    "append_workspace_filter",
    "build_turn_search_text",
    "escape_like",
    "format_iso_utc",
    "normalize_cursor_key",
    "normalize_date_bound",
]
