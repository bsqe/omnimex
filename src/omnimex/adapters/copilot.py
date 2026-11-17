"""Adapter for GitHub Copilot Chat (VS Code native/SQLite/exports) and Copilot CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    IncrementalParseResult,
    extract_text_blocks,
    ide_user_roots,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
    parse_timestamp,
    query_vscdb_rows,
    read_workspace_json,
    safe_is_dir,
    safe_is_file,
    safe_read_text,
    safe_stat,
    safe_walk_files,
    sqlite_effective_mtime_ns,
)
from omnimex.core.models import (
    IngestCursor,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_IDE_VARIANTS = ("Code", "Code - Insiders", "VSCodium")


class CopilotAdapter(BaseAgentAdapter):
    """Ingest GitHub Copilot Chat VS Code stores and Copilot CLI event logs."""

    agent_id = "copilot"
    display_name = "GitHub Copilot"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        return [*ide_user_roots(_IDE_VARIANTS), Path.home() / ".copilot"]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        if "/chatsessions/" in norm and path.suffix.lower() in {".json", ".jsonl"}:
            return True
        if "github.copilot-chat" in norm and path.suffix.lower() == ".json":
            return True
        if ".copilot/" in norm and (path.name == "events.jsonl" or path.suffix.lower() == ".json"):
            return True
        return path.name == "state.vscdb" and ("code" in norm or "vscodium" in norm)

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_copilot_file(p: Path) -> bool:
            norm = normalize_source_path(p).lower()
            if p.name == "state.vscdb":
                return True
            if "/chatsessions/" in norm and p.suffix.lower() in {".json", ".jsonl"}:
                return True
            if "github.copilot-chat" in norm and p.suffix.lower() == ".json":
                return True
            if p.name == "events.jsonl" and ("session-state" in norm or ".copilot" in norm):
                return True
            return bool("history-session-state" in norm and p.suffix.lower() == ".json")

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if _is_copilot_file(root):
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue

            for p in safe_walk_files(root, predicate=_is_copilot_file, errors=self.last_errors):
                key = normalize_source_path(p)
                if key not in seen:
                    seen.add(key)
                    discovered.append(p)

        discovered.sort(key=self._source_priority)
        return discovered

    @staticmethod
    def _source_priority(path: Path) -> tuple[int, str]:
        norm = normalize_source_path(path).lower()
        if path.name == "events.jsonl" or ("/chatsessions/" in norm and path.suffix.lower() == ".jsonl"):
            return (0, norm)
        if "/chatsessions/" in norm and path.suffix.lower() == ".json":
            return (1, norm)
        if path.suffix.lower() == ".json":
            return (2, norm)
        return (3, norm)

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        if path.name == "state.vscdb":
            return self._parse_vscdb(path)

        raw = safe_read_text(path, errors=self.last_errors)
        if raw is None:
            return []

        if path.suffix.lower() == ".jsonl":
            records: list[tuple[int, dict[str, Any]]] = []
            for idx, line in enumerate(raw.splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append((idx, obj))
            return self._parse_jsonl_records(path, records, 0, {})

        return self._parse_json_file(path, raw)

    def parse_file_incremental(
        self,
        path: Path,
        cursor: IngestCursor | None = None,
        *,
        start_turn_index: int | None = None,
    ) -> IncrementalParseResult:
        if path.suffix.lower() == ".jsonl":
            return self._parse_jsonl_incremental(
                path, cursor, self._parse_jsonl_records, start_turn_index=start_turn_index
            )
        return super().parse_file_incremental(path, cursor, start_turn_index=start_turn_index)

    def _parse_vscdb(self, db_path: Path) -> list[NormalizedSession]:
        rows = query_vscdb_rows(
            db_path,
            "ItemTable",
            exact_keys=("interactive.sessions",),
            errors=self.last_errors,
        )
        fallback_ws = read_workspace_json(db_path)
        mtime_ns = sqlite_effective_mtime_ns(db_path)
        norm_src = normalize_source_path(db_path)
        sessions: list[NormalizedSession] = []

        for _, raw_val in rows:
            try:
                payload = json.loads(raw_val)
            except json.JSONDecodeError:
                continue
            items: list[Any] = []
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = list(payload.values())

            for item in items:
                if not isinstance(item, dict):
                    continue
                if not self._is_copilot_vscdb_session(item):
                    continue
                session = self._parse_vscode_session_obj(
                    item, source_path=norm_src, fallback_ws=fallback_ws, mtime_ns=mtime_ns
                )
                if session is not None:
                    sessions.append(session)
        return sessions

    @staticmethod
    def _is_copilot_vscdb_session(item: dict[str, Any]) -> bool:
        responder = item.get("responderUsername")
        if responder is None:
            return "requests" in item
        return "copilot" in str(responder).lower()

    def _parse_json_file(self, path: Path, raw: str) -> list[NormalizedSession]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        fallback_ws = read_workspace_json(path)
        st = safe_stat(path)
        mtime_ns = st.st_mtime_ns if st else 0
        norm_src = normalize_source_path(path)

        if isinstance(data, list):
            sessions = [
                s
                for item in data
                if isinstance(item, dict)
                and (s := self._parse_generic_obj(item, path.stem, norm_src, fallback_ws, mtime_ns))
            ]
            return sessions

        if isinstance(data, dict):
            if isinstance(data.get("conversations"), list):
                return [
                    s
                    for item in data["conversations"]
                    if isinstance(item, dict)
                    and (s := self._parse_generic_obj(item, path.stem, norm_src, fallback_ws, mtime_ns))
                ]
            session = self._parse_generic_obj(data, path.stem, norm_src, fallback_ws, mtime_ns)
            return [session] if session else []
        return []

    def _parse_generic_obj(
        self,
        obj: dict[str, Any],
        default_id: str,
        source_path: str,
        fallback_ws: str | None,
        mtime_ns: int,
    ) -> NormalizedSession | None:
        if "requests" in obj:
            return self._parse_vscode_session_obj(
                obj, source_path=source_path, fallback_ws=fallback_ws, mtime_ns=mtime_ns
            )

        ext_id = str(obj.get("id") or obj.get("sessionId") or default_id)
        sid = f"{self.agent_id}:{ext_id}"
        workspace = normalize_workspace_path(
            obj.get("workspaceFolder") or obj.get("workspacePath") or obj.get("cwd") or fallback_ws
        )
        turns: list[NormalizedTurn] = []

        if isinstance(obj.get("turns"), list):
            for t in obj["turns"]:
                if not isinstance(t, dict):
                    continue
                req = t.get("request") if isinstance(t.get("request"), dict) else {}
                resp = t.get("response") if isinstance(t.get("response"), dict) else {}
                req_msg = str(req.get("message") or "").strip()
                if req_msg:
                    turns.append(
                        NormalizedTurn(
                            session_id=sid,
                            turn_index=len(turns),
                            role="user",
                            content=req_msg,
                            timestamp=parse_timestamp(req.get("timestamp")),
                        )
                    )
                resp_msg = str(resp.get("message") or "").strip()
                if resp_msg:
                    turns.append(
                        NormalizedTurn(
                            session_id=sid,
                            turn_index=len(turns),
                            role="assistant",
                            content=resp_msg,
                            timestamp=parse_timestamp(resp.get("timestamp")),
                        )
                    )
        else:
            raw_msgs = obj.get("messages") or obj.get("history") or []
            if isinstance(raw_msgs, list):
                for m in raw_msgs:
                    if not isinstance(m, dict):
                        continue
                    role = str(m.get("role") or "user").lower()
                    text = str(m.get("content") or m.get("result") or "").strip()
                    if not text:
                        continue
                    turns.append(
                        NormalizedTurn(
                            session_id=sid,
                            turn_index=len(turns),
                            role=role,
                            content=text,
                            timestamp=parse_timestamp(m.get("timestamp") or m.get("time")),
                        )
                    )

        if not turns:
            return None

        started_at, updated_at = normalize_session_timestamps(
            turns, fallback_mtime_ns=mtime_ns
        )
        title = str(obj.get("title") or obj.get("customTitle") or "").strip()
        return NormalizedSession(
            id=sid,
            agent=self.agent_id,
            external_id=ext_id,
            title=title or make_title(turns[0].content, fallback=ext_id),
            workspace=workspace,
            source_path=source_path,
            started_at=started_at,
            updated_at=updated_at,
            turn_count=len(turns),
            turns=turns,
        )

    def _parse_vscode_session_obj(
        self,
        obj: dict[str, Any],
        *,
        source_path: str,
        fallback_ws: str | None,
        mtime_ns: int,
    ) -> NormalizedSession | None:
        ext_id = str(obj.get("sessionId") or obj.get("id") or "copilot-session")
        sid = f"{self.agent_id}:{ext_id}"
        turns: list[NormalizedTurn] = []

        for req in obj.get("requests") or []:
            if not isinstance(req, dict):
                continue
            ts = parse_timestamp(req.get("timestamp"))
            msg_obj = req.get("message")
            user_text = (
                str(msg_obj.get("text") or "").strip()
                if isinstance(msg_obj, dict)
                else str(msg_obj or "").strip()
            )
            if user_text:
                turns.append(
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=len(turns),
                        role="user",
                        content=user_text,
                        timestamp=ts,
                    )
                )

            resp_texts: list[str] = []
            tool_calls: list[ToolCallRecord] = []
            for item in req.get("response") or []:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("value"), str) and item["value"].strip():
                    resp_texts.append(item["value"].strip())
                elif item.get("kind") == "toolInvocationSerialized":
                    specific = item.get("toolSpecificData")
                    raw_in = specific.get("rawInput", {}) if isinstance(specific, dict) else {}
                    tool_calls.append(
                        ToolCallRecord(
                            id=str(item.get("toolCallId") or f"tc_{len(tool_calls)}"),
                            name=str(item.get("toolId") or "tool"),
                            arguments=raw_in,
                            status="ok",
                        )
                    )
            if resp_texts or tool_calls:
                turns.append(
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=len(turns),
                        role="assistant",
                        content="\n".join(resp_texts),
                        tool_calls=tool_calls,
                        timestamp=ts,
                    )
                )

        if not turns:
            return None

        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=parse_timestamp(obj.get("creationDate")),
            updated_at=parse_timestamp(obj.get("lastMessageDate")),
            fallback_mtime_ns=mtime_ns,
        )
        title = str(obj.get("customTitle") or "").strip()
        return NormalizedSession(
            id=sid,
            agent=self.agent_id,
            external_id=ext_id,
            title=title or make_title(turns[0].content, fallback=ext_id),
            workspace=fallback_ws,
            source_path=source_path,
            started_at=started_at,
            updated_at=updated_at,
            turn_count=len(turns),
            turns=turns,
        )

    def _parse_jsonl_records(
        self,
        path: Path,
        records: list[tuple[int, dict[str, Any]]],
        start_turn_index: int,
        initial_meta: dict[str, Any],
    ) -> list[NormalizedSession]:
        if not records:
            return []

        fallback_ws = read_workspace_json(path)
        st = safe_stat(path)
        mtime_ns = st.st_mtime_ns if st else 0
        norm_src = normalize_source_path(path)

        # VS Code chatSessions append-log format (kind: 0, v: {...})
        if any("kind" in rec and isinstance(rec.get("v"), dict) for _, rec in records):
            sessions: list[NormalizedSession] = []
            for _, rec in records:
                if isinstance(rec.get("v"), dict):
                    s = self._parse_vscode_session_obj(
                        rec["v"], source_path=norm_src, fallback_ws=fallback_ws, mtime_ns=mtime_ns
                    )
                    if s is not None:
                        sessions.append(s)
            return sessions

        # Copilot CLI Chronicle / flat events.jsonl
        external_id = str(initial_meta.get("sid") or "")
        workspace: str | None = initial_meta.get("ws") or fallback_ws
        title = str(initial_meta.get("title") or "")

        for _, rec in records:
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            if not external_id:
                sid_val = rec.get("session_id") or data.get("sessionId")
                if isinstance(sid_val, str) and sid_val:
                    external_id = sid_val
            if workspace is None:
                cwd_val = rec.get("cwd") or data.get("cwd")
                if isinstance(cwd_val, str) and cwd_val:
                    workspace = normalize_workspace_path(cwd_val)

        if not external_id:
            external_id = path.parent.name if path.name == "events.jsonl" else path.stem

        session_id = f"{self.agent_id}:{external_id}"
        turns: list[NormalizedTurn] = []
        turn_idx = start_turn_index

        for _, rec in records:
            etype = str(rec.get("type") or "").lower()
            if etype in {"session.start", "session.end"}:
                continue
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            ts = parse_timestamp(rec.get("timestamp"))

            if etype in {"user.message", "userpromptsubmitted"}:
                raw_text = data.get("content") or rec.get("prompt") or rec.get("content")
                text, _ = extract_text_blocks(raw_text)
                if not text:
                    continue
                if not title:
                    title = make_title(text, fallback=external_id)
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_idx,
                        role="user",
                        content=text,
                        timestamp=ts,
                    )
                )
                turn_idx += 1
            elif etype in {"assistant.message", "assistantresponse"}:
                raw_text = (
                    data.get("content")
                    or rec.get("output")
                    or rec.get("result")
                    or rec.get("content")
                )
                text, _ = extract_text_blocks(raw_text)
                tools = self._extract_cli_tool_requests(data.get("toolRequests"))
                if not text and not tools:
                    continue
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_idx,
                        role="assistant",
                        content=text,
                        tool_calls=tools,
                        timestamp=ts,
                    )
                )
                turn_idx += 1

        if not turns:
            return []

        started_at, updated_at = normalize_session_timestamps(
            turns, fallback_mtime_ns=mtime_ns
        )
        return [
            NormalizedSession(
                id=session_id,
                agent=self.agent_id,
                external_id=external_id,
                title=title or make_title(turns[0].content, fallback=external_id),
                workspace=workspace,
                source_path=norm_src,
                started_at=started_at,
                updated_at=updated_at,
                turn_count=len(turns),
                turns=turns,
            )
        ]

    @staticmethod
    def _extract_cli_tool_requests(raw_requests: Any) -> list[ToolCallRecord]:
        if not isinstance(raw_requests, list):
            return []
        out: list[ToolCallRecord] = []
        for idx, item in enumerate(raw_requests):
            if isinstance(item, dict):
                out.append(
                    ToolCallRecord(
                        id=str(item.get("id") or f"tc_{idx}"),
                        name=str(item.get("name") or "tool"),
                        arguments=item.get("input") or item.get("arguments") or {},
                        status="ok",
                    )
                )
        return out


__all__ = ["CopilotAdapter"]
