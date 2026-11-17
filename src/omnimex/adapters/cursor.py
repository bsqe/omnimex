"""Adapter for Cursor IDE state databases (``state.vscdb``) and transcripts."""

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
    parse_vscdb_bubbles,
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
    normalize_workspace_path,
)


class CursorAdapter(BaseAgentAdapter):
    """Ingest Cursor Composer and AI chat histories safely via read-only temp copy."""

    agent_id = "cursor"
    display_name = "Cursor"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        return [*ide_user_roots(["Cursor"]), Path.home() / ".cursor"]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return (
            path.name == "state.vscdb"
            and ("cursor" in norm or "globalstorage" in norm or "workspacestorage" in norm)
        ) or (path.suffix.lower() == ".jsonl" and "cursor" in norm)

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_cursor_source(p: Path) -> bool:
            return p.name == "state.vscdb" or p.suffix.lower() == ".jsonl"

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if _is_cursor_source(root):
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue

            scan_targets = [root]
            if safe_is_dir(root / "User"):
                scan_targets = [root / "User"]

            for target in scan_targets:
                for p in safe_walk_files(
                    target, predicate=_is_cursor_source, errors=self.last_errors
                ):
                    key = normalize_source_path(p)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(p)

        discovered.sort()
        return discovered

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        if path.suffix.lower() == ".jsonl":
            raw = safe_read_text(path, errors=self.last_errors)
            if raw is None:
                return []
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

        if path.name != "state.vscdb":
            return []

        return self._parse_vscdb(path)

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
        sessions: list[NormalizedSession] = []
        fallback_ws = read_workspace_json(db_path)
        mtime_ns = sqlite_effective_mtime_ns(db_path)
        norm_src = normalize_source_path(db_path)

        kv_rows = query_vscdb_rows(
            db_path,
            "cursorDiskKV",
            key_prefixes=("composerData:", "bubbleId:"),
            errors=self.last_errors,
        )
        composers: list[tuple[str, dict[str, Any]]] = []
        bubble_map: dict[str, dict[str, Any]] = {}

        for key, raw_val in kv_rows:
            try:
                parsed = json.loads(raw_val)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if key.startswith("composerData:"):
                cid = key.split(":", 1)[1]
                composers.append((cid, parsed))
            elif key.startswith("bubbleId:"):
                bubble_map[key] = parsed

        for cid, data in composers:
            session = self._build_composer_session(
                cid, data, bubble_map, norm_src, fallback_ws, mtime_ns
            )
            if session is not None:
                sessions.append(session)

        item_rows = query_vscdb_rows(
            db_path,
            "ItemTable",
            exact_keys=("workbench.panel.aichat.view.aichat.chatdata",),
            errors=self.last_errors,
        )
        for _key, raw_val in item_rows:
            try:
                parsed = json.loads(raw_val)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("tabs"), list):
                for tab_idx, tab in enumerate(parsed["tabs"]):
                    if not isinstance(tab, dict) or not isinstance(tab.get("bubbles"), list):
                        continue
                    ext_id = str(tab.get("tabId") or f"{db_path.parent.name}-tab-{tab_idx}")
                    sid = f"{self.agent_id}:{ext_id}"
                    turns, bubble_ws = parse_vscdb_bubbles(tab["bubbles"], sid)
                    if not turns:
                        continue
                    started_at, updated_at = normalize_session_timestamps(
                        turns,
                        started_at=parse_timestamp(tab.get("timestamp")),
                        fallback_mtime_ns=mtime_ns,
                    )
                    sessions.append(
                        NormalizedSession(
                            id=sid,
                            agent=self.agent_id,
                            external_id=ext_id,
                            title=str(tab.get("chatTitle") or make_title(turns[0].content, ext_id)),
                            workspace=bubble_ws or fallback_ws,
                            source_path=norm_src,
                            started_at=started_at,
                            updated_at=updated_at,
                            turn_count=len(turns),
                            turns=turns,
                        )
                    )

        return sessions

    def _build_composer_session(
        self,
        composer_id: str,
        data: dict[str, Any],
        bubble_map: dict[str, dict[str, Any]],
        norm_src: str,
        fallback_ws: str | None,
        mtime_ns: int,
    ) -> NormalizedSession | None:
        ext_id = str(data.get("composerId") or composer_id)
        sid = f"{self.agent_id}:{ext_id}"
        model_cfg = data.get("modelConfig")
        default_model = (
            str(model_cfg.get("modelName"))
            if isinstance(model_cfg, dict) and isinstance(model_cfg.get("modelName"), str)
            else None
        )

        bubbles: list[dict[str, Any]] = []
        headers = data.get("fullConversationHeadersOnly")
        if isinstance(headers, list) and headers:
            for hdr in headers:
                if not isinstance(hdr, dict):
                    continue
                bid = hdr.get("bubbleId")
                if not isinstance(bid, str):
                    continue
                row = bubble_map.get(f"bubbleId:{ext_id}:{bid}") or bubble_map.get(
                    f"bubbleId:{composer_id}:{bid}"
                )
                if row is not None:
                    bubbles.append(row)
        elif isinstance(data.get("tabs"), list):
            for tab in data["tabs"]:
                if isinstance(tab, dict) and isinstance(tab.get("bubbles"), list):
                    bubbles.extend(b for b in tab["bubbles"] if isinstance(b, dict))

        turns: list[NormalizedTurn] = []
        detected_ws: str | None = None
        if bubbles:
            turns, detected_ws = parse_vscdb_bubbles(bubbles, sid)
        else:
            simple_text = str(data.get("text") or data.get("richText") or "").strip()
            if simple_text:
                turns = [
                    NormalizedTurn(
                        session_id=sid,
                        turn_index=0,
                        role="user",
                        content=simple_text,
                        timestamp=parse_timestamp(data.get("createdAt")),
                    )
                ]

        if not turns:
            return None

        session_model = default_model or next(
            (t.model for t in turns if t.model), None
        )
        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=parse_timestamp(data.get("createdAt")),
            updated_at=parse_timestamp(data.get("lastUpdatedAt")),
            fallback_mtime_ns=mtime_ns,
        )
        title = str(data.get("name") or "").strip() or make_title(
            turns[0].content, fallback=ext_id
        )
        return NormalizedSession(
            id=sid,
            agent=self.agent_id,
            external_id=ext_id,
            title=title,
            workspace=detected_ws or fallback_ws,
            source_path=norm_src,
            started_at=started_at,
            updated_at=updated_at,
            turn_count=len(turns),
            model=session_model,
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
        external_id = str(initial_meta.get("sid") or "")
        workspace: str | None = initial_meta.get("ws")
        title = str(initial_meta.get("title") or "")
        for _, rec in records:
            if not external_id and isinstance(rec.get("sessionId"), str):
                external_id = rec["sessionId"]
            if workspace is None and isinstance(rec.get("workspace"), str):
                workspace = normalize_workspace_path(rec["workspace"])
        if not external_id:
            external_id = path.stem

        session_id = f"{self.agent_id}:{external_id}"
        turns: list[NormalizedTurn] = []
        turn_idx = start_turn_index

        for _, rec in records:
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
            role = str(msg.get("role") or rec.get("type") or "user").lower()
            text, thinking = extract_text_blocks(msg.get("content") or msg.get("text"))
            if not text and role == "tool":
                text = str(msg.get("tool_output") or "").strip()
            if not text:
                continue
            if not title and role == "user":
                title = make_title(text, fallback=external_id)
            turns.append(
                NormalizedTurn(
                    session_id=session_id,
                    turn_index=turn_idx,
                    role=role,
                    content=text,
                    thinking=thinking,
                    timestamp=parse_timestamp(rec.get("timestamp")),
                )
            )
            turn_idx += 1

        if not turns:
            return []
        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns, fallback_mtime_ns=st.st_mtime_ns if st else None
        )
        return [
            NormalizedSession(
                id=session_id,
                agent=self.agent_id,
                external_id=external_id,
                title=title or make_title(turns[0].content, fallback=external_id),
                workspace=workspace,
                source_path=normalize_source_path(path),
                started_at=started_at,
                updated_at=updated_at,
                turn_count=len(turns),
                turns=turns,
            )
        ]


__all__ = ["CursorAdapter"]
