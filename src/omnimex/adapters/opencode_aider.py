"""Adapters for OpenCode (SQLite 1.x/2.x + JSON tree) and Aider (``.aider.chat.history.md``)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    extract_text_blocks,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
    open_sqlite_readonly,
    parse_timestamp,
    safe_is_dir,
    safe_is_file,
    safe_read_text,
    safe_scandir,
    safe_stat,
    safe_walk_files,
    sqlite_effective_mtime_ns,
)
from omnimex.core.models import (
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_AIDER_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"})


class OpenCodeAdapter(BaseAgentAdapter):
    """Ingest OpenCode SQLite (1.x Drizzle + 2.x V2) and JSON storage trees."""

    agent_id = "opencode"
    display_name = "OpenCode"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        home = Path.home()
        roots: list[Path] = []
        if local_app := os.getenv("LOCALAPPDATA"):
            roots.append(Path(local_app) / "opencode")
        if xdg_data := os.getenv("XDG_DATA_HOME"):
            roots.append(Path(xdg_data).expanduser() / "opencode")
        roots.extend([home / ".local" / "share" / "opencode", home / ".opencode"])
        return roots

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return (
            path.name.startswith("opencode") and path.suffix.lower() in {".db", ".sqlite"}
        ) or ("opencode" in norm and "/session/" in norm and path.suffix.lower() == ".json")

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _add(p: Path) -> None:
            key = normalize_source_path(p)
            if key not in seen and safe_is_file(p):
                seen.add(key)
                discovered.append(p)

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                _add(root)
                continue
            if not safe_is_dir(root):
                continue

            for db_name in ("opencode.db", "opencode-dev.db"):
                candidate_db = root / db_name
                if safe_is_file(candidate_db):
                    _add(candidate_db)

            for db_file in safe_walk_files(
                root,
                predicate=lambda p: p.suffix.lower() in {".db", ".sqlite"},
                errors=self.last_errors,
            ):
                _add(db_file)

            session_dirs = [root / "storage" / "session", root / "session"]
            for sdir in session_dirs:
                if safe_is_dir(sdir):
                    for ses_json in safe_walk_files(
                        sdir,
                        predicate=lambda p: p.suffix.lower() == ".json",
                        errors=self.last_errors,
                    ):
                        _add(ses_json)

        discovered.sort()
        return discovered

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        if path.suffix.lower() in {".db", ".sqlite"}:
            return self._parse_sqlite(path)
        if path.suffix.lower() == ".json":
            return self._parse_json_session(path)
        return []

    def _parse_sqlite(self, db_path: Path) -> list[NormalizedSession]:
        sessions: list[NormalizedSession] = []
        seen_ids: set[str] = set()
        norm_src = normalize_source_path(db_path)
        mtime_ns = sqlite_effective_mtime_ns(db_path)

        try:
            with open_sqlite_readonly(db_path) as conn:
                tables = {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if {"session_v2", "session_message"} <= tables:
                    for s in self._read_v2_sqlite(conn, norm_src, mtime_ns):
                        seen_ids.add(s.external_id)
                        sessions.append(s)
                if {"session", "message", "part"} <= tables:
                    for s in self._read_v1_sqlite(conn, norm_src, mtime_ns, skip_ids=seen_ids):
                        sessions.append(s)
        except (sqlite3.Error, OSError) as exc:
            self.last_errors.append(f"sqlite_read_failed: {norm_src} ({exc})")
        return sessions

    def _read_v2_sqlite(
        self, conn: sqlite3.Connection, source_path: str, mtime_ns: int
    ) -> list[NormalizedSession]:
        out: list[NormalizedSession] = []
        s_rows = conn.execute(
            "SELECT id, directory, title, time_created, time_updated FROM session_v2 "
            "ORDER BY time_created ASC"
        ).fetchall()
        for s_row in s_rows:
            ext_id = str(s_row["id"])
            sid = f"{self.agent_id}:{ext_id}"
            m_rows = conn.execute(
                "SELECT id, type, seq, time_created, data FROM session_message "
                "WHERE session_id = ? ORDER BY seq ASC",
                (ext_id,),
            ).fetchall()

            # Respect location-switched reset boundary
            reset_after_idx = -1
            for idx, m_row in enumerate(m_rows):
                if m_row["type"] == "location-switched":
                    reset_after_idx = idx
            active_rows = m_rows[reset_after_idx + 1 :] if reset_after_idx >= 0 else m_rows

            turns: list[NormalizedTurn] = []
            session_model: str | None = None
            for m_row in active_rows:
                mtype = str(m_row["type"])
                if mtype not in {"user", "assistant"}:
                    continue
                try:
                    data = json.loads(m_row["data"])
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue

                if mtype == "user":
                    text = str(data.get("text") or "").strip()
                    thinking = None
                else:
                    text, thinking = extract_text_blocks(data.get("content"))
                    if not text and isinstance(data.get("text"), str):
                        text = data["text"].strip()

                model_obj = data.get("model")
                turn_model = (
                    str(model_obj.get("id"))
                    if isinstance(model_obj, dict) and isinstance(model_obj.get("id"), str)
                    else None
                )
                if turn_model:
                    session_model = turn_model
                if not text and not thinking:
                    continue
                turns.append(
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=len(turns),
                        role=mtype,
                        content=text,
                        thinking=thinking,
                        timestamp=parse_timestamp(m_row["time_created"]),
                        model=turn_model,
                    )
                )

            if not turns:
                continue
            started_at, updated_at = normalize_session_timestamps(
                turns,
                started_at=parse_timestamp(s_row["time_created"]),
                updated_at=parse_timestamp(s_row["time_updated"]),
                fallback_mtime_ns=mtime_ns,
            )
            title = str(s_row["title"] or "").strip()
            out.append(
                NormalizedSession(
                    id=sid,
                    agent=self.agent_id,
                    external_id=ext_id,
                    title=title or make_title(turns[0].content, fallback=ext_id),
                    workspace=normalize_workspace_path(s_row["directory"]),
                    source_path=source_path,
                    started_at=started_at,
                    updated_at=updated_at,
                    turn_count=len(turns),
                    model=session_model,
                    turns=turns,
                )
            )
        return out

    def _read_v1_sqlite(
        self,
        conn: sqlite3.Connection,
        source_path: str,
        mtime_ns: int,
        *,
        skip_ids: set[str],
    ) -> list[NormalizedSession]:
        out: list[NormalizedSession] = []
        s_rows = conn.execute(
            "SELECT id, directory, title, time_created, time_updated FROM session "
            "ORDER BY time_created ASC"
        ).fetchall()
        for s_row in s_rows:
            ext_id = str(s_row["id"])
            if ext_id in skip_ids:
                continue
            sid = f"{self.agent_id}:{ext_id}"
            m_rows = conn.execute(
                "SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created ASC",
                (ext_id,),
            ).fetchall()

            turns: list[NormalizedTurn] = []
            session_model: str | None = None
            for m_row in m_rows:
                try:
                    mdata = json.loads(m_row["data"])
                except json.JSONDecodeError:
                    mdata = {}
                role = str(mdata.get("role") or "user").lower()
                turn_model = mdata.get("modelID") if isinstance(mdata.get("modelID"), str) else None
                if turn_model:
                    session_model = turn_model

                p_rows = conn.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC",
                    (m_row["id"],),
                ).fetchall()
                parts = []
                for p_row in p_rows:
                    try:
                        pdata = json.loads(p_row["data"])
                        if isinstance(pdata, dict):
                            parts.append(pdata)
                    except json.JSONDecodeError:
                        continue
                text, thinking, tools = self._flatten_parts(parts)
                if not text and not thinking and not tools:
                    continue
                turns.append(
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=len(turns),
                        role=role,
                        content=text,
                        thinking=thinking,
                        tool_calls=tools,
                        timestamp=parse_timestamp(m_row["time_created"]),
                        model=turn_model,
                    )
                )

            if not turns:
                continue
            started_at, updated_at = normalize_session_timestamps(
                turns,
                started_at=parse_timestamp(s_row["time_created"]),
                updated_at=parse_timestamp(s_row["time_updated"]),
                fallback_mtime_ns=mtime_ns,
            )
            out.append(
                NormalizedSession(
                    id=sid,
                    agent=self.agent_id,
                    external_id=ext_id,
                    title=str(s_row["title"] or make_title(turns[0].content, ext_id)),
                    workspace=normalize_workspace_path(s_row["directory"]),
                    source_path=source_path,
                    started_at=started_at,
                    updated_at=updated_at,
                    turn_count=len(turns),
                    model=session_model,
                    turns=turns,
                )
            )
        return out

    def _parse_json_session(self, ses_path: Path) -> list[NormalizedSession]:
        raw = safe_read_text(ses_path, errors=self.last_errors)
        if not raw:
            return []
        try:
            ses = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(ses, dict):
            return []

        ext_id = str(ses.get("id") or ses_path.stem)
        sid = f"{self.agent_id}:{ext_id}"
        storage_root = self._locate_storage_root(ses_path)
        msg_dir = storage_root / "message" / ext_id if storage_root else None
        part_root = storage_root / "part" if storage_root else None

        msg_files: list[Path] = []
        if msg_dir and safe_is_dir(msg_dir):
            msg_files = [
                Path(e.path)
                for e in safe_scandir(msg_dir, errors=self.last_errors)
                if e.name.endswith(".json")
            ]

        parsed_msgs: list[tuple[float, NormalizedTurn]] = []
        session_model: str | None = None
        for mfile in msg_files:
            mraw = safe_read_text(mfile, errors=self.last_errors)
            if not mraw:
                continue
            try:
                mdata = json.loads(mraw)
            except json.JSONDecodeError:
                continue
            if not isinstance(mdata, dict):
                continue

            msg_id = str(mdata.get("id") or mfile.stem)
            role = str(mdata.get("role") or "user").lower()
            turn_model = mdata.get("modelID") if isinstance(mdata.get("modelID"), str) else None
            if turn_model:
                session_model = turn_model
            time_obj = mdata.get("time") if isinstance(mdata.get("time"), dict) else {}
            ts = parse_timestamp(time_obj.get("created") or mdata.get("created"))

            parts: list[dict[str, Any]] = []
            if part_root and safe_is_dir(part_root / msg_id):
                for entry in safe_scandir(part_root / msg_id, errors=self.last_errors):
                    if not entry.name.endswith(".json"):
                        continue
                    praw = safe_read_text(Path(entry.path), errors=self.last_errors)
                    if not praw:
                        continue
                    try:
                        pdata = json.loads(praw)
                        if isinstance(pdata, dict):
                            parts.append(pdata)
                    except json.JSONDecodeError:
                        continue

            text, thinking, tools = self._flatten_parts(parts)
            if not text and not thinking and not tools:
                continue
            parsed_msgs.append(
                (
                    ts.timestamp() if ts else 0.0,
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=0,
                        role=role,
                        content=text,
                        thinking=thinking,
                        tool_calls=tools,
                        timestamp=ts,
                        model=turn_model,
                    ),
                )
            )

        if not parsed_msgs:
            return []

        parsed_msgs.sort(key=lambda pair: pair[0])
        turns = [
            t.model_copy(update={"turn_index": idx, "id": f"{sid}#t{idx}"})
            for idx, (_, t) in enumerate(parsed_msgs)
        ]

        ses_time = ses.get("time") if isinstance(ses.get("time"), dict) else {}
        st = safe_stat(ses_path)
        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=parse_timestamp(ses_time.get("created")),
            updated_at=parse_timestamp(ses_time.get("updated")),
            fallback_mtime_ns=st.st_mtime_ns if st else None,
        )
        return [
            NormalizedSession(
                id=sid,
                agent=self.agent_id,
                external_id=ext_id,
                title=str(ses.get("title") or make_title(turns[0].content, ext_id)),
                workspace=normalize_workspace_path(ses.get("directory")),
                source_path=normalize_source_path(ses_path),
                started_at=started_at,
                updated_at=updated_at,
                turn_count=len(turns),
                model=session_model,
                turns=turns,
            )
        ]

    @staticmethod
    def _locate_storage_root(ses_path: Path) -> Path | None:
        for parent in ses_path.parents:
            if parent.name == "session":
                return parent.parent
        return None

    @staticmethod
    def _flatten_parts(
        parts: Sequence[dict[str, Any]],
    ) -> tuple[str, str | None, list[ToolCallRecord]]:
        texts: list[str] = []
        thoughts: list[str] = []
        tools: list[ToolCallRecord] = []

        for idx, part in enumerate(parts):
            ptype = str(part.get("type") or "text").lower()
            if ptype == "text":
                txt = str(part.get("text") or "").strip()
                if txt:
                    texts.append(txt)
            elif ptype == "reasoning":
                txt = str(part.get("text") or "").strip()
                if txt:
                    thoughts.append(txt)
            elif ptype == "tool":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                out = state.get("output")
                tools.append(
                    ToolCallRecord(
                        id=str(part.get("id") or f"tc_{idx}"),
                        name=str(part.get("tool") or part.get("name") or "tool"),
                        arguments=state.get("input") or {},
                        output=str(out) if out is not None else None,
                        status="ok",
                    )
                )

        return "\n".join(texts), ("\n".join(thoughts) if thoughts else None), tools


class AiderAdapter(BaseAgentAdapter):
    """Ingest Aider ``.aider.chat.history.md`` Markdown dialogue logs."""

    agent_id = "aider"
    display_name = "Aider"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        candidates = [Path.home() / ".aider.chat.history.md", Path.cwd() / ".aider.chat.history.md"]
        return [p for p in candidates if safe_is_file(p)]

    def matches_path(self, path: Path) -> bool:
        return path.name == ".aider.chat.history.md"

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if root.name == ".aider.chat.history.md":
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue
            for p in safe_walk_files(
                root,
                predicate=lambda cand: cand.name == ".aider.chat.history.md",
                skip_dir_names=_AIDER_SKIP_DIRS,
                errors=self.last_errors,
            ):
                key = normalize_source_path(p)
                if key not in seen:
                    seen.add(key)
                    discovered.append(p)

        discovered.sort()
        return discovered

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        raw = safe_read_text(path, errors=self.last_errors)
        if not raw or not raw.strip():
            return []

        norm_src = normalize_source_path(path)
        workspace = normalize_workspace_path(path.parent)
        ext_id = f"{path.parent.name}-{path.stem}".strip("-") or "aider"
        session_id = f"{self.agent_id}:{ext_id}"
        started_ts = None

        blocks: list[tuple[str, list[str]]] = []
        current_role: str | None = None
        current_lines: list[str] = []

        for line in raw.splitlines():
            if line.startswith("# aider chat started at "):
                ts_str = line[len("# aider chat started at ") :].strip()
                started_ts = started_ts or parse_timestamp(ts_str)
                continue
            if line.startswith("#### "):
                continue
            if line.startswith("> ") or line == ">":
                user_line = line[2:] if line.startswith("> ") else ""
                if current_role != "user":
                    if current_role and current_lines:
                        blocks.append((current_role, current_lines))
                    current_role = "user"
                    current_lines = [user_line]
                else:
                    current_lines.append(user_line)
            else:
                if current_role == "user":
                    if current_lines:
                        blocks.append(("user", current_lines))
                    current_role = "assistant"
                    current_lines = [line]
                elif current_role is None:
                    if line.strip():
                        current_role = "system"
                        current_lines = [line]
                else:
                    current_lines.append(line)

        if current_role and current_lines:
            blocks.append((current_role, current_lines))

        turns: list[NormalizedTurn] = []
        for role, lines in blocks:
            text = "\n".join(lines).strip()
            if not text:
                continue
            turns.append(
                NormalizedTurn(
                    session_id=session_id,
                    turn_index=len(turns),
                    role=role,
                    content=text,
                    timestamp=started_ts,
                )
            )

        if not turns:
            return []

        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=started_ts,
            fallback_mtime_ns=st.st_mtime_ns if st else None,
        )
        return [
            NormalizedSession(
                id=session_id,
                agent=self.agent_id,
                external_id=ext_id,
                title=f"Aider Chat: {path.parent.name}",
                workspace=workspace,
                source_path=norm_src,
                started_at=started_at,
                updated_at=updated_at,
                turn_count=len(turns),
                turns=turns,
            )
        ]


__all__ = ["AiderAdapter", "OpenCodeAdapter"]
