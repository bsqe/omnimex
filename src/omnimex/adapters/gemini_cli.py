"""Adapter for Gemini CLI session files (JSON and event-sourced JSONL)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    IncrementalParseResult,
    extract_text_blocks,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
    parse_timestamp,
    safe_is_dir,
    safe_is_file,
    safe_read_text,
    safe_stat,
    safe_walk_files,
)
from omnimex.core.models import (
    IngestCursor,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_AGENTS_MD_WS_RE = re.compile(r"^#\s*AGENTS\.md instructions for\s+([^\r\n]+)", re.MULTILINE)
_WORKING_DIR_WS_RE = re.compile(r"(?im)^Working directory:\s*(.+)$")


class GeminiCliAdapter(BaseAgentAdapter):
    """Ingest Gemini CLI sessions from ``~/.gemini/tmp/<hash>/chats/session-*``."""

    agent_id = "gemini_cli"
    display_name = "Gemini CLI"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        if env_home := os.getenv("GEMINI_HOME"):
            return [Path(env_home).expanduser() / "tmp"]
        return [Path.home() / ".gemini" / "tmp"]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return (
            "antigravity" not in norm
            and path.name.startswith("session-")
            and path.suffix.lower() in {".json", ".jsonl"}
            and ("/chats/" in norm or ".gemini/tmp" in norm)
        )

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_gemini_session(p: Path) -> bool:
            norm = normalize_source_path(p).lower()
            if "antigravity" in norm:
                return False
            return p.name.startswith("session-") and p.suffix.lower() in {".json", ".jsonl"}

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if _is_gemini_session(root):
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue

            for p in safe_walk_files(
                root,
                predicate=_is_gemini_session,
                skip_dir_names=frozenset({"antigravity", "antigravity-cli"}),
                errors=self.last_errors,
            ):
                key = normalize_source_path(p)
                if key not in seen:
                    seen.add(key)
                    discovered.append(p)

        discovered.sort()
        return discovered

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        raw_text = safe_read_text(path, errors=self.last_errors)
        if raw_text is None:
            return []

        if path.suffix.lower() == ".json":
            return self._parse_legacy_json(path, raw_text)

        records: list[tuple[int, dict[str, Any]]] = []
        for idx, line in enumerate(raw_text.splitlines(), start=1):
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

    def parse_file_incremental(
        self,
        path: Path,
        cursor: IngestCursor | None = None,
        *,
        start_turn_index: int | None = None,
    ) -> IncrementalParseResult:
        if path.suffix.lower() == ".json":
            return super().parse_file_incremental(
                path, cursor, start_turn_index=start_turn_index
            )
        return self._parse_jsonl_incremental(
            path, cursor, self._parse_jsonl_records, start_turn_index=start_turn_index
        )

    def _parse_legacy_json(self, path: Path, raw_text: str) -> list[NormalizedSession]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return []

        external_id = str(data.get("sessionId") or path.stem)
        session_id = f"{self.agent_id}:{external_id}"
        turns, workspace, title = self._convert_messages(
            data["messages"], session_id, start_turn_index=0, external_id=external_id
        )
        if not turns:
            return []

        if workspace is None:
            workspace = self._fallback_hash_workspace(path)

        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=parse_timestamp(data.get("startTime")),
            updated_at=parse_timestamp(data.get("lastUpdated")),
            fallback_mtime_ns=st.st_mtime_ns if st else None,
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
        started_hint = None
        updated_hint = None
        raw_messages: list[dict[str, Any]] = []

        for _, rec in records:
            if not external_id and isinstance(rec.get("sessionId"), str):
                external_id = rec["sessionId"]
            if started_hint is None and rec.get("startTime"):
                started_hint = parse_timestamp(rec.get("startTime"))

            patch = rec.get("$set")
            if isinstance(patch, dict):
                if patch.get("lastUpdated"):
                    updated_hint = parse_timestamp(patch.get("lastUpdated"))
                if isinstance(patch.get("messages"), list):
                    for msg in patch["messages"]:
                        if isinstance(msg, dict):
                            raw_messages.append(msg)
                continue

            if "type" in rec and ("content" in rec or "toolCalls" in rec):
                raw_messages.append(rec)

        if not external_id:
            external_id = path.stem

        session_id = f"{self.agent_id}:{external_id}"
        turns, detected_ws, detected_title = self._convert_messages(
            raw_messages, session_id, start_turn_index=start_turn_index, external_id=external_id
        )
        if not turns:
            return []

        if workspace is None:
            workspace = detected_ws or self._fallback_hash_workspace(path)
        if not title:
            title = detected_title

        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns,
            started_at=started_hint,
            updated_at=updated_hint,
            fallback_mtime_ns=st.st_mtime_ns if st else None,
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

    def _convert_messages(
        self,
        raw_messages: Sequence[dict[str, Any]],
        session_id: str,
        *,
        start_turn_index: int,
        external_id: str,
    ) -> tuple[list[NormalizedTurn], str | None, str]:
        turns: list[NormalizedTurn] = []
        workspace: str | None = None
        title = ""
        turn_idx = start_turn_index

        for msg in raw_messages:
            raw_type = str(msg.get("type") or msg.get("role") or "user").lower()
            role = "assistant" if raw_type in {"model", "gemini"} else raw_type
            text, thinking = extract_text_blocks(msg.get("content"))

            tool_calls: list[ToolCallRecord] = []
            if isinstance(msg.get("toolCalls"), list):
                for tc_idx, tc in enumerate(msg["toolCalls"]):
                    if isinstance(tc, dict):
                        tool_calls.append(
                            ToolCallRecord(
                                id=str(tc.get("id") or f"tc_{tc_idx}"),
                                name=str(tc.get("name") or "tool"),
                                arguments=tc.get("args") or tc.get("arguments") or {},
                                output=str(tc["result"]) if tc.get("result") is not None else None,
                                status="ok",
                            )
                        )

            if not text and not thinking and not tool_calls:
                continue

            if workspace is None and text:
                workspace = self._extract_workspace_from_text(text)
            if not title and role == "user" and text:
                title = make_title(text, fallback=external_id)

            turns.append(
                NormalizedTurn(
                    session_id=session_id,
                    turn_index=turn_idx,
                    role=role,
                    content=text,
                    thinking=thinking,
                    tool_calls=tool_calls,
                    timestamp=parse_timestamp(msg.get("timestamp")),
                )
            )
            turn_idx += 1

        return turns, workspace, title

    @staticmethod
    def _extract_workspace_from_text(text: str) -> str | None:
        if m := _AGENTS_MD_WS_RE.search(text):
            return normalize_workspace_path(m.group(1).strip())
        if m := _WORKING_DIR_WS_RE.search(text):
            return normalize_workspace_path(m.group(1).strip())
        return None

    @staticmethod
    def _fallback_hash_workspace(path: Path) -> str | None:
        if path.parent.name == "chats" and len(path.parents) >= 2:
            return normalize_source_path(path.parents[1])
        return None


__all__ = ["GeminiCliAdapter"]
