"""Adapter for Windsurf (Codeium) IDE sessions and native memories."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    IncrementalParseResult,
    build_native_memory_id,
    extract_text_blocks,
    ide_user_roots,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
    parse_timestamp,
    parse_vscdb_bubbles,
    parse_yaml_frontmatter,
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
    MemoryCard,
    MemoryCategory,
    NormalizedSession,
    NormalizedTurn,
    normalize_workspace_path,
)


class WindsurfAdapter(BaseAgentAdapter):
    """Ingest Windsurf IDE ``state.vscdb``, Cascade JSON exports, and Codeium memories."""

    agent_id = "windsurf"
    display_name = "Windsurf"
    supports_native_memories = True

    def default_roots(self) -> list[Path]:
        home = Path.home()
        return [
            *ide_user_roots(["Windsurf"]),
            home / ".codeium" / "windsurf",
            home / ".windsurf",
        ]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return ("windsurf" in norm or "codeium" in norm) and (
            path.name == "state.vscdb" or path.suffix.lower() in {".json", ".jsonl"}
        )

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_windsurf_source(p: Path) -> bool:
            if p.suffix.lower() == ".pb":
                return False
            if p.name == "state.vscdb":
                return True
            if p.name in {"workspace.json", "mcp_config.json"}:
                return False
            return p.suffix.lower() in {".json", ".jsonl"}

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if _is_windsurf_source(root):
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue

            for p in safe_walk_files(
                root,
                predicate=_is_windsurf_source,
                skip_dir_names=frozenset({"memories", "global_workflows"}),
                errors=self.last_errors,
            ):
                key = normalize_source_path(p)
                if key not in seen:
                    seen.add(key)
                    discovered.append(p)

        discovered.sort()
        return discovered

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

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        if isinstance(data, dict):
            session = self._session_from_payload(
                ext_id=str(data.get("id") or data.get("cascadeId") or path.stem),
                payload=data,
                source_path=normalize_source_path(path),
                fallback_ws=None,
                mtime_ns=st.st_mtime_ns if (st := safe_stat(path)) else 0,
            )
            return [session] if session else []
        return []

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

        for table in ("cursorDiskKV", "ItemTable"):
            rows = query_vscdb_rows(
                db_path,
                table,
                key_prefixes=("codeium.cascade", "windsurf.chat", "composerData:"),
                exact_keys=(
                    "workbench.panel.aichat.view.aichat.chatdata",
                    "interactive.sessions",
                ),
                errors=self.last_errors,
            )
            for key, raw_val in rows:
                try:
                    parsed = json.loads(raw_val)
                except json.JSONDecodeError:
                    continue
                items = parsed if isinstance(parsed, list) else [parsed]
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    ext_id = str(
                        item.get("id")
                        or item.get("composerId")
                        or item.get("sessionId")
                        or (key.split(":", 1)[1] if ":" in key else f"{key}-{idx}")
                    )
                    session = self._session_from_payload(
                        ext_id=ext_id,
                        payload=item,
                        source_path=norm_src,
                        fallback_ws=fallback_ws,
                        mtime_ns=mtime_ns,
                    )
                    if session is not None:
                        sessions.append(session)
        return sessions

    def _session_from_payload(
        self,
        *,
        ext_id: str,
        payload: dict[str, Any],
        source_path: str,
        fallback_ws: str | None,
        mtime_ns: int,
    ) -> NormalizedSession | None:
        sid = f"{self.agent_id}:{ext_id}"
        workspace = normalize_workspace_path(
            payload.get("workspace") or payload.get("cwd") or fallback_ws
        )
        turns: list[NormalizedTurn] = []

        if isinstance(payload.get("tabs"), list):
            for tab in payload["tabs"]:
                if isinstance(tab, dict) and isinstance(tab.get("bubbles"), list):
                    t_batch, bubble_ws = parse_vscdb_bubbles(
                        tab["bubbles"], sid, start_idx=len(turns)
                    )
                    turns.extend(t_batch)
                    if workspace is None and bubble_ws:
                        workspace = bubble_ws
        else:
            raw_msgs = payload.get("messages") or payload.get("steps") or []
            if isinstance(raw_msgs, list):
                for item in raw_msgs:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or item.get("type") or "user").lower()
                    text, thinking = extract_text_blocks(
                        item.get("content") or item.get("text") or item.get("message")
                    )
                    if not text:
                        continue
                    turns.append(
                        NormalizedTurn(
                            session_id=sid,
                            turn_index=len(turns),
                            role=role,
                            content=text,
                            thinking=thinking,
                            timestamp=parse_timestamp(item.get("timestamp")),
                            model=item.get("model"),
                        )
                    )

        if not turns:
            return None

        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=parse_timestamp(payload.get("createdAt")),
            updated_at=parse_timestamp(payload.get("lastUpdatedAt")),
            fallback_mtime_ns=mtime_ns,
        )
        title = str(payload.get("title") or payload.get("name") or "").strip()
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

    def _parse_jsonl_records(
        self,
        path: Path,
        records: list[tuple[int, dict[str, Any]]],
        start_turn_index: int,
        initial_meta: dict[str, Any],
    ) -> list[NormalizedSession]:
        if not records:
            return []
        external_id = str(initial_meta.get("sid") or path.stem)
        sid = f"{self.agent_id}:{external_id}"
        workspace: str | None = initial_meta.get("ws")
        turns: list[NormalizedTurn] = []
        turn_idx = start_turn_index

        for _, rec in records:
            if workspace is None and isinstance(rec.get("workspace"), str):
                workspace = normalize_workspace_path(rec["workspace"])
            role = str(rec.get("role") or "user").lower()
            text, thinking = extract_text_blocks(rec.get("content") or rec.get("text"))
            if not text:
                continue
            turns.append(
                NormalizedTurn(
                    session_id=sid,
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
                id=sid,
                agent=self.agent_id,
                external_id=external_id,
                title=str(initial_meta.get("title") or make_title(turns[0].content, external_id)),
                workspace=workspace,
                source_path=normalize_source_path(path),
                started_at=started_at,
                updated_at=updated_at,
                turn_count=len(turns),
                turns=turns,
            )
        ]

    def discover_native_memories(self, roots: Sequence[Path] | None = None) -> list[MemoryCard]:
        """Discover Windsurf/Codeium memories under ``memories/*.md``."""
        cards: list[MemoryCard] = []
        seen: set[str] = set()

        for root in self.resolve_roots(roots):
            mem_candidates = [
                root / "memories",
                root / "codeium" / "windsurf" / "memories",
            ]
            for mem_dir in mem_candidates:
                if not safe_is_dir(mem_dir):
                    continue
                for md_path in safe_walk_files(
                    mem_dir,
                    predicate=lambda p: p.suffix.lower() == ".md",
                    errors=self.last_errors,
                ):
                    norm = normalize_source_path(md_path)
                    if norm in seen:
                        continue
                    seen.add(norm)
                    raw = safe_read_text(md_path, errors=self.last_errors)
                    if not raw or not raw.strip():
                        continue
                    meta, body = parse_yaml_frontmatter(raw)
                    if not body:
                        continue
                    raw_cat = str(meta.get("category") or "preference").lower()
                    cat: MemoryCategory = (
                        raw_cat
                        if raw_cat in {"preference", "decision", "pitfall", "fact"}
                        else "preference"
                    )  # type: ignore[assignment]
                    tags = [str(t) for t in meta.get("tags", [])] if isinstance(meta.get("tags"), list) else ["windsurf"]
                    st = safe_stat(md_path)
                    ts = parse_timestamp(st.st_mtime if st else None)
                    cards.append(
                        MemoryCard(
                            id=build_native_memory_id("windsurf", norm),
                            title=str(meta.get("title") or md_path.stem),
                            category=cat,
                            content=body,
                            tags=tags,
                            distill_status="heuristic",
                            origin="native_import",
                            reviewed=False,
                            source_agent=self.agent_id,
                            native_source_path=norm,
                            created_at=ts or parse_timestamp(1),
                            updated_at=ts or parse_timestamp(1),
                        )
                    )
        return cards


__all__ = ["WindsurfAdapter"]
