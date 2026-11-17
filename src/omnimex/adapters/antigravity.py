"""Adapter for Antigravity (agy) brain transcript logs."""

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
    safe_scandir,
    safe_stat,
)
from omnimex.core.models import (
    IngestCursor,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>")
_SETTINGS_MODEL_RE = re.compile(
    r"<USER_SETTINGS_CHANGE>[\s\S]*?Model Selection`?\s+from\s+.+?\s+to\s+([^\n<]+?)(?=\.\s*(?:No need|\n|<|$)|<|\n|$)",
    re.IGNORECASE,
)
_WORKSPACE_META_RE = re.compile(r"(?im)^Workspace:\s*(.+)$")
_STRIP_WRAPPERS_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|EPHEMERAL_MESSAGE)\b[^>]*>"
    r"[\s\S]*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|EPHEMERAL_MESSAGE)>",
    re.IGNORECASE,
)
_TOOL_STEP_TYPES = frozenset(
    {"VIEW_FILE", "RUN_COMMAND", "WRITE_FILE", "EDIT_FILE", "SEARCH", "GREP_SEARCH"}
)


class AntigravityAdapter(BaseAgentAdapter):
    """Ingest Antigravity transcripts from ``~/.gemini/antigravity/brain/<id>``."""

    agent_id = "antigravity"
    display_name = "Antigravity"
    supports_native_memories = False

    def default_roots(self) -> list[Path]:
        if env_home := os.getenv("ANTIGRAVITY_HOME"):
            return [Path(env_home).expanduser()]
        gemini = Path.home() / ".gemini"
        return [gemini / "antigravity", gemini / "antigravity-cli"]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return path.name in {"transcript.jsonl", "transcript_full.jsonl"} and (
            "antigravity" in norm or ".system_generated/logs" in norm
        )

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _add(candidate: Path) -> None:
            key = normalize_source_path(candidate)
            if key not in seen and safe_is_file(candidate):
                seen.add(key)
                discovered.append(candidate)

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if root.name in {"transcript.jsonl", "transcript_full.jsonl"}:
                    _add(root)
                continue
            if not safe_is_dir(root, errors=self.last_errors):
                continue

            brain_dirs: list[Path] = []
            if root.name == "brain":
                brain_dirs.append(root)
            else:
                candidate_brain = root / "brain"
                if safe_is_dir(candidate_brain, errors=self.last_errors):
                    brain_dirs.append(candidate_brain)
                for sub in ("antigravity", "antigravity-cli"):
                    nested = root / sub / "brain"
                    if safe_is_dir(nested, errors=self.last_errors):
                        brain_dirs.append(nested)

            # Handle direct session directory passed as root
            direct_logs = root / ".system_generated" / "logs"
            if safe_is_dir(direct_logs, errors=self.last_errors):
                self._collect_logs_dir(direct_logs, _add)

            for brain_dir in brain_dirs:
                for entry in safe_scandir(brain_dir, errors=self.last_errors):
                    session_dir = Path(entry.path)
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError as exc:
                        self.last_errors.append(
                            f"permission_denied: {normalize_source_path(session_dir)} ({exc})"
                        )
                        continue
                    for sub_entry in safe_scandir(session_dir, errors=self.last_errors):
                        if sub_entry.name == ".system_generated":
                            logs_dir = Path(sub_entry.path) / "logs"
                            if safe_is_dir(logs_dir, errors=self.last_errors):
                                self._collect_logs_dir(logs_dir, _add)

        discovered.sort()
        return discovered

    def _collect_logs_dir(self, logs_dir: Path, add_fn: Any) -> None:
        primary = logs_dir / "transcript.jsonl"
        full = logs_dir / "transcript_full.jsonl"
        if safe_is_file(primary):
            add_fn(primary)
        elif safe_is_file(full):
            add_fn(full)

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        raw_text = safe_read_text(path, errors=self.last_errors)
        if raw_text is None:
            return []

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

        return self._parse_records(path, records, start_turn_index=0, initial_meta={})

    def parse_file_incremental(
        self,
        path: Path,
        cursor: IngestCursor | None = None,
        *,
        start_turn_index: int | None = None,
    ) -> IncrementalParseResult:
        return self._parse_jsonl_incremental(
            path,
            cursor,
            self._parse_records,
            start_turn_index=start_turn_index,
        )

    def _load_full_step_map(
        self,
        path: Path,
        needed_steps: set[int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        if path.name != "transcript.jsonl":
            return {}
        if needed_steps is not None and not needed_steps:
            return {}
        sibling = path.with_name("transcript_full.jsonl")
        if not safe_is_file(sibling):
            return {}
        raw = safe_read_text(sibling, errors=self.last_errors)
        if raw is None:
            return {}
        steps: dict[int, dict[str, Any]] = {}
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("step_index"), int):
                step_idx = obj["step_index"]
                if needed_steps is None or step_idx in needed_steps:
                    steps[step_idx] = obj
        return steps

    def _hydrate_record(
        self,
        record: dict[str, Any],
        full_steps: dict[int, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if not record.get("truncated_fields") or not full_steps:
            return record
        step_idx = record.get("step_index")
        if isinstance(step_idx, int) and step_idx in full_steps:
            return {**record, **full_steps[step_idx]}
        return record

    def _parse_records(
        self,
        path: Path,
        records: list[tuple[int, dict[str, Any]]],
        start_turn_index: int,
        initial_meta: dict[str, Any],
    ) -> list[NormalizedSession]:
        if not records:
            return []

        needed_steps = {
            rec["step_index"]
            for _, rec in records
            if rec.get("truncated_fields") and isinstance(rec.get("step_index"), int)
        }
        full_steps = self._load_full_step_map(path, needed_steps) if needed_steps else None

        external_id = str(initial_meta.get("sid") or self._infer_session_id(path))
        session_id = f"{self.agent_id}:{external_id}"
        workspace: str | None = initial_meta.get("ws")
        active_model: str | None = initial_meta.get("model")
        title: str = str(initial_meta.get("title") or "")

        turns: list[NormalizedTurn] = []
        turn_index = start_turn_index

        for _, raw_record in records:
            rec = self._hydrate_record(raw_record, full_steps)
            step_type = str(rec.get("type") or "").upper()
            source = str(rec.get("source") or "").upper()
            ts = parse_timestamp(rec.get("created_at"))
            raw_content = rec.get("content")
            content_str, content_thinking = (
                (raw_content, None)
                if isinstance(raw_content, str)
                else extract_text_blocks(raw_content)
            )
            thinking = rec.get("thinking") if isinstance(rec.get("thinking"), str) else content_thinking
            if thinking is not None:
                thinking = thinking.strip() or None

            if step_type == "CONVERSATION_HISTORY" and not content_str.strip():
                continue

            if step_type == "USER_INPUT" or source == "USER_EXPLICIT":
                if settings_match := _SETTINGS_MODEL_RE.search(content_str):
                    active_model = settings_match.group(1).strip()
                if workspace is None and (ws_match := _WORKSPACE_META_RE.search(content_str)):
                    workspace = normalize_workspace_path(ws_match.group(1).strip())
                cleaned = self._extract_user_content(content_str)
                if not cleaned:
                    continue
                if not title:
                    title = make_title(cleaned, fallback=external_id)
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role="user",
                        content=cleaned,
                        timestamp=ts,
                    )
                )
                turn_index += 1
                continue

            if step_type in _TOOL_STEP_TYPES or (
                step_type == "GENERIC" and source == "MODEL"
            ):
                tool_calls = self._extract_tool_calls(
                    rec.get("tool_calls"),
                    fallback_name=step_type.lower() if step_type != "GENERIC" else None,
                    output=content_str.strip() or None,
                )
                if not content_str.strip() and not tool_calls:
                    continue
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role="tool",
                        content=content_str.strip(),
                        thinking=thinking,
                        tool_calls=tool_calls,
                        timestamp=ts,
                        model=active_model,
                    )
                )
                turn_index += 1
                continue

            if step_type == "PLANNER_RESPONSE" or source == "MODEL":
                tool_calls = self._extract_tool_calls(rec.get("tool_calls"))
                text = content_str.strip()
                if not text and not thinking and not tool_calls:
                    continue
                token_count = self._sum_tokens(rec)
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role="assistant",
                        content=text,
                        thinking=thinking,
                        tool_calls=tool_calls,
                        timestamp=ts,
                        model=active_model,
                        token_count=token_count,
                    )
                )
                turn_index += 1
                continue

            if step_type in {"SYSTEM_MESSAGE", "EPHEMERAL_MESSAGE"} or source == "SYSTEM":
                text = content_str.strip()
                if not text:
                    continue
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role="system",
                        content=text,
                        timestamp=ts,
                    )
                )
                turn_index += 1

        if not turns:
            return []

        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns,
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
                model=active_model,
                turns=turns,
            )
        ]

    @staticmethod
    def _infer_session_id(path: Path) -> str:
        # Canonical: .../brain/<session_id>/.system_generated/logs/transcript.jsonl
        if len(path.parents) >= 3 and path.parent.name == "logs":
            return path.parents[2].name
        return path.stem

    @staticmethod
    def _extract_user_content(raw: str) -> str:
        if req := _USER_REQUEST_RE.search(raw):
            return req.group(1).strip()
        stripped = _STRIP_WRAPPERS_RE.sub("", raw).strip()
        return stripped

    @staticmethod
    def _extract_tool_calls(
        raw_calls: Any,
        *,
        fallback_name: str | None = None,
        output: str | None = None,
    ) -> list[ToolCallRecord]:
        records: list[ToolCallRecord] = []
        if isinstance(raw_calls, list):
            for idx, item in enumerate(raw_calls):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("tool_name") or fallback_name or "tool")
                args = item.get("arguments")
                if args is None:
                    args = item.get("args")
                if args is None:
                    args = {}
                records.append(
                    ToolCallRecord(
                        id=str(item.get("id") or f"tc_{idx}"),
                        name=name,
                        arguments=args,
                        output=output,
                        status="ok",
                    )
                )
        elif fallback_name:
            records.append(
                ToolCallRecord(
                    id="tc_0",
                    name=fallback_name,
                    arguments={},
                    output=output,
                    status="ok",
                )
            )
        return records

    @staticmethod
    def _sum_tokens(record: dict[str, Any]) -> int | None:
        in_tok = record.get("input_tokens")
        out_tok = record.get("output_tokens")
        if isinstance(in_tok, int) or isinstance(out_tok, int):
            return (in_tok if isinstance(in_tok, int) else 0) + (
                out_tok if isinstance(out_tok, int) else 0
            )
        return None


__all__ = ["AntigravityAdapter"]
