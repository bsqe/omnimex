"""Adapter for OpenAI Codex rollouts, SQLite thread/memory metadata, and Markdown memories."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    IncrementalParseResult,
    build_native_memory_id,
    extract_text_blocks,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
    open_sqlite_readonly,
    parse_timestamp,
    parse_yaml_frontmatter,
    safe_is_dir,
    safe_is_file,
    safe_read_text,
    safe_stat,
    safe_walk_files,
)
from omnimex.core.models import (
    IngestCursor,
    MemoryCard,
    MemoryCategory,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_ENV_CONTEXT_RE = re.compile(
    r"^\s*<environment_context>\s*[\s\S]*?</environment_context>\s*$"
)
_USER_INSTR_RE = re.compile(
    r"^\s*<user_instructions>\s*[\s\S]*?</user_instructions>\s*$"
)
_AGENTS_ENVELOPE_RE = re.compile(
    r"^# AGENTS\.md instructions(?: for [^\r\n]+)?\n\n<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>"
    r"(?:\n<environment_context>[\s\S]*?</environment_context>)?\s*$"
)
_SKIP_EVENT_TYPES = frozenset(
    {
        "task_started",
        "task_complete",
        "token_usage_record",
        "thread_settings_applied",
        "item_completed",
        "world_state",
        "turn_context",
        "compacted",
    }
)


class CodexAdapter(BaseAgentAdapter):
    """Ingest Codex rollout transcripts, SQLite thread metadata, and native memories."""

    agent_id = "codex"
    display_name = "Codex"
    supports_native_memories = True

    def default_roots(self) -> list[Path]:
        if env_home := os.getenv("CODEX_HOME"):
            return [Path(env_home).expanduser()]
        return [Path.home() / ".codex"]

    def matches_path(self, path: Path) -> bool:
        name = path.name
        return name.startswith("rollout-") and path.suffix.lower() in {".jsonl", ".json"}

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_rollout(p: Path) -> bool:
            return p.name.startswith("rollout-") and p.suffix.lower() in {".jsonl", ".json"}

        for root in self.resolve_roots(roots):
            if safe_is_file(root):
                if _is_rollout(root):
                    key = normalize_source_path(root)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(root)
                continue
            if not safe_is_dir(root):
                continue

            sub_roots = [root / "sessions", root / "archived_sessions"]
            existing_subs = [sr for sr in sub_roots if safe_is_dir(sr)]
            targets = existing_subs if existing_subs else [root]
            for target in targets:
                for file_path in safe_walk_files(
                    target, predicate=_is_rollout, errors=self.last_errors
                ):
                    key = normalize_source_path(file_path)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(file_path)

        discovered.sort()
        return discovered

    def _load_thread_meta(self, roots: Sequence[Path]) -> dict[str, dict[str, Any]]:
        meta_by_id: dict[str, dict[str, Any]] = {}
        for root in roots:
            db_candidates = [root / "sqlite" / "state_5.sqlite", root / "state_5.sqlite"]
            for db_path in db_candidates:
                if not safe_is_file(db_path):
                    continue
                try:
                    with open_sqlite_readonly(db_path) as conn:
                        rows = conn.execute(
                            "SELECT id, cwd, title, model, git_branch FROM threads"
                        ).fetchall()
                        for row in rows:
                            meta_by_id[str(row["id"])] = dict(row)
                except (sqlite3.Error, OSError) as exc:
                    self.last_errors.append(f"sqlite_read_failed: {db_path} ({exc})")
        return meta_by_id

    def _roots_for_file(self, path: Path) -> list[Path]:
        if self._explicit_roots is not None:
            return list(self._explicit_roots)
        for parent in path.parents:
            if parent.name in {"sessions", "archived_sessions"}:
                return [parent.parent]
        return [path.parent]

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

        return self._parse_records(path, records, start_turn_index=0, initial_meta={})

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
            path,
            cursor,
            self._parse_records,
            start_turn_index=start_turn_index,
        )

    def _parse_legacy_json(self, path: Path, raw_text: str) -> list[NormalizedSession]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return []

        session_meta = data.get("session") if isinstance(data.get("session"), dict) else {}
        external_id = str(session_meta.get("id") or path.stem)
        session_id = f"{self.agent_id}:{external_id}"
        workspace = normalize_workspace_path(session_meta.get("cwd"))
        turns: list[NormalizedTurn] = []
        title = ""

        for idx, item in enumerate(data["items"]):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").lower()
            text, thinking = extract_text_blocks(item.get("content"))
            if not text:
                continue
            if not title and role == "user":
                title = make_title(text, fallback=external_id)
            turns.append(
                NormalizedTurn(
                    session_id=session_id,
                    turn_index=idx,
                    role=role,
                    content=text,
                    thinking=thinking,
                    timestamp=parse_timestamp(item.get("timestamp")),
                )
            )
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

    def _parse_records(
        self,
        path: Path,
        records: list[tuple[int, dict[str, Any]]],
        start_turn_index: int,
        initial_meta: dict[str, Any],
    ) -> list[NormalizedSession]:
        if not records:
            return []

        external_id: str = str(initial_meta.get("sid") or "")
        workspace: str | None = initial_meta.get("ws")
        model: str | None = initial_meta.get("model")
        title: str = str(initial_meta.get("title") or "")
        tags: list[str] = []

        for _, rec in records:
            rtype = str(rec.get("type") or "")
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rtype == "session_meta":
                if not external_id and isinstance(payload.get("id"), str):
                    external_id = payload["id"]
                if workspace is None and isinstance(payload.get("cwd"), str):
                    workspace = normalize_workspace_path(payload["cwd"])
                git_info = payload.get("git")
                if isinstance(git_info, dict) and isinstance(git_info.get("branch"), str):
                    branch = git_info["branch"]
                    if branch and branch not in tags:
                        tags.append(branch)
            elif rtype == "turn_context":
                if workspace is None and isinstance(payload.get("cwd"), str):
                    workspace = normalize_workspace_path(payload["cwd"])
                if model is None and isinstance(payload.get("model"), str):
                    model = payload["model"]

        if not external_id:
            external_id = path.stem

        thread_meta = self._load_thread_meta(self._roots_for_file(path)).get(external_id, {})
        if workspace is None and thread_meta.get("cwd"):
            workspace = normalize_workspace_path(thread_meta["cwd"])
        if model is None and thread_meta.get("model"):
            model = str(thread_meta["model"])
        if not title and thread_meta.get("title"):
            title = str(thread_meta["title"])

        session_id = f"{self.agent_id}:{external_id}"
        turns: list[NormalizedTurn] = []
        turn_index = start_turn_index
        pending_thinking: str | None = None
        saw_leading_developer = False
        saw_real_dialogue = start_turn_index > 0
        last_logical_msg: tuple[str, str, str] | None = None

        for _, rec in records:
            rtype = str(rec.get("type") or "")
            if rtype in _SKIP_EVENT_TYPES or rtype == "session_meta":
                continue
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else None
            if payload is None:
                continue

            ts = parse_timestamp(rec.get("timestamp"))
            if rtype == "response_item":
                ptype = str(payload.get("type") or "message")
                if ptype == "reasoning":
                    thought = self._extract_reasoning(payload)
                    if thought:
                        pending_thinking = (
                            f"{pending_thinking}\n{thought}" if pending_thinking else thought
                        )
                    continue

                if ptype in {"function_call", "custom_tool_call"}:
                    saw_real_dialogue = True
                    tc = self._build_response_tool_call(payload, freeform=(ptype == "custom_tool_call"))
                    if tc is None:
                        continue
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role="assistant",
                            content=f"[Tool: {tc.name}]",
                            thinking=pending_thinking,
                            tool_calls=[tc],
                            timestamp=ts,
                            model=model,
                        )
                    )
                    pending_thinking = None
                    turn_index += 1
                    last_logical_msg = None
                    continue

                if ptype == "function_call_output":
                    saw_real_dialogue = True
                    out = payload.get("output")
                    out_text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    out_text = out_text.strip()
                    call_id = str(payload.get("call_id") or "")
                    self._attach_tool_output(turns, call_id, out_text)
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role="tool",
                            content=out_text,
                            timestamp=ts,
                        )
                    )
                    turn_index += 1
                    last_logical_msg = None
                    continue

                if ptype == "message":
                    role_raw = str(payload.get("role") or "assistant").lower()
                    if role_raw == "developer":
                        if not saw_real_dialogue:
                            saw_leading_developer = True
                        continue
                    text, inline_thinking = extract_text_blocks(payload.get("content"))
                    if not text:
                        continue
                    if (
                        role_raw == "user"
                        and saw_leading_developer
                        and not saw_real_dialogue
                        and self._is_host_envelope(text)
                    ):
                        continue
                    saw_real_dialogue = True
                    ev_id = str(payload.get("id") or "")
                    if ev_id and last_logical_msg == (ev_id, role_raw, text):
                        continue
                    last_logical_msg = (ev_id, role_raw, text) if ev_id else None

                    if role_raw == "user" and not title:
                        title = make_title(text, fallback=external_id)
                    thought_val = pending_thinking or inline_thinking
                    if role_raw == "assistant":
                        pending_thinking = None
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role=role_raw,
                            content=text,
                            thinking=thought_val if role_raw == "assistant" else None,
                            timestamp=ts,
                            model=model if role_raw == "assistant" else None,
                        )
                    )
                    turn_index += 1
                continue

            if rtype == "event_msg":
                etype = str(payload.get("type") or "")
                if etype in _SKIP_EVENT_TYPES:
                    continue
                if etype == "agent_reasoning":
                    thought = str(payload.get("text") or "").strip()
                    if thought:
                        pending_thinking = (
                            f"{pending_thinking}\n{thought}" if pending_thinking else thought
                        )
                    continue
                if etype == "token_count":
                    total = self._extract_token_count(payload)
                    if total is not None:
                        for prev in reversed(turns):
                            if prev.role == "assistant":
                                prev.token_count = total
                                break
                    continue
                if etype in {"user_message", "agent_message"}:
                    role_raw = "user" if etype == "user_message" else "assistant"
                    text = str(payload.get("message") or payload.get("text") or "").strip()
                    if not text:
                        continue
                    saw_real_dialogue = True
                    ev_id = str(payload.get("id") or "")
                    if ev_id and last_logical_msg == (ev_id, role_raw, text):
                        continue
                    last_logical_msg = (ev_id, role_raw, text) if ev_id else None
                    if role_raw == "user" and not title:
                        title = make_title(text, fallback=external_id)
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role=role_raw,
                            content=text,
                            thinking=pending_thinking if role_raw == "assistant" else None,
                            timestamp=ts,
                            model=model if role_raw == "assistant" else None,
                        )
                    )
                    if role_raw == "assistant":
                        pending_thinking = None
                    turn_index += 1
                    continue
                if etype == "tool_call":
                    saw_real_dialogue = True
                    name = str(payload.get("name") or "tool")
                    args = payload.get("input") if payload.get("input") is not None else {}
                    tc = ToolCallRecord(
                        id=str(payload.get("call_id") or f"tc_{turn_index}"),
                        name=name,
                        arguments=args,
                        status="ok",
                    )
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role="assistant",
                            content=f"[Tool: {name}]",
                            tool_calls=[tc],
                            timestamp=ts,
                            model=model,
                        )
                    )
                    turn_index += 1

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
                model=model,
                tags=tags,
                turns=turns,
            )
        ]

    def discover_native_memories(self, roots: Sequence[Path] | None = None) -> list[MemoryCard]:
        """Discover native Codex Markdown memories and ``memories_1.sqlite`` stage1 outputs."""
        resolved = self.resolve_roots(roots)
        cards: list[MemoryCard] = []
        seen_ids: set[str] = set()

        for root in resolved:
            mem_dir = root if root.name == "memories" else root / "memories"
            if safe_is_dir(mem_dir):
                for md_path in safe_walk_files(
                    mem_dir,
                    predicate=lambda p: p.suffix.lower() == ".md",
                    errors=self.last_errors,
                ):
                    raw = safe_read_text(md_path, errors=self.last_errors)
                    if raw is None or not raw.strip():
                        continue
                    meta, body = parse_yaml_frontmatter(raw)
                    if not body:
                        continue
                    norm_md = normalize_source_path(md_path)
                    card_id = build_native_memory_id("codex", norm_md)
                    if card_id in seen_ids:
                        continue
                    seen_ids.add(card_id)
                    category = self._infer_memory_category(meta, body)
                    st = safe_stat(md_path)
                    ts = parse_timestamp(st.st_mtime if st else None)
                    cards.append(
                        MemoryCard(
                            id=card_id,
                            title=str(meta.get("title") or md_path.stem),
                            category=category,
                            content=body,
                            tags=["codex"],
                            distill_status="heuristic",
                            origin="native_import",
                            reviewed=False,
                            source_agent=self.agent_id,
                            native_source_path=norm_md,
                            created_at=ts or parse_timestamp(1),
                            updated_at=ts or parse_timestamp(1),
                        )
                    )

            for db_path in (root / "sqlite" / "memories_1.sqlite", root / "memories_1.sqlite"):
                if not safe_is_file(db_path):
                    continue
                try:
                    with open_sqlite_readonly(db_path) as conn:
                        rows = conn.execute(
                            "SELECT thread_id, raw_memory, rollout_summary, rollout_slug, generated_at "
                            "FROM stage1_outputs"
                        ).fetchall()
                        for row in rows:
                            tid = str(row["thread_id"])
                            card_id = f"native_codex_sqlite_{tid}"
                            if card_id in seen_ids:
                                continue
                            raw_mem = str(row["raw_memory"] or "").strip()
                            summary = str(row["rollout_summary"] or "").strip()
                            content = f"{summary}\n\n{raw_mem}".strip() if summary and raw_mem else (summary or raw_mem)
                            if not content:
                                continue
                            seen_ids.add(card_id)
                            gen_ts = parse_timestamp(row["generated_at"])
                            cards.append(
                                MemoryCard(
                                    id=card_id,
                                    title=str(row["rollout_slug"] or tid),
                                    category="fact",
                                    content=content,
                                    tags=["codex", "stage1"],
                                    distill_status="heuristic",
                                    origin="native_import",
                                    reviewed=False,
                                    source_session_id=f"{self.agent_id}:{tid}",
                                    source_agent=self.agent_id,
                                    native_source_path=normalize_source_path(db_path),
                                    created_at=gen_ts or parse_timestamp(1),
                                    updated_at=gen_ts or parse_timestamp(1),
                                )
                            )
                except (sqlite3.Error, OSError) as exc:
                    self.last_errors.append(f"sqlite_read_failed: {db_path} ({exc})")

        return cards

    @staticmethod
    def _is_host_envelope(text: str) -> bool:
        return bool(
            _ENV_CONTEXT_RE.match(text)
            or _USER_INSTR_RE.match(text)
            or _AGENTS_ENVELOPE_RE.match(text)
        )

    @staticmethod
    def _extract_reasoning(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("summary"), list):
            parts = [
                str(item.get("text", "")).strip()
                for item in payload["summary"]
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
        if isinstance(payload.get("text"), str):
            return payload["text"].strip()
        return ""

    @staticmethod
    def _build_response_tool_call(
        payload: dict[str, Any], *, freeform: bool
    ) -> ToolCallRecord | None:
        name = str(payload.get("name") or "tool")
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        if freeform:
            args: dict[str, Any] | str = str(payload.get("input") or "")
        else:
            raw_args = payload.get("arguments")
            if isinstance(raw_args, str):
                try:
                    parsed = json.loads(raw_args)
                    args = parsed if isinstance(parsed, dict) else raw_args
                except json.JSONDecodeError:
                    args = raw_args
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
        return ToolCallRecord(id=call_id, name=name, arguments=args, status="ok")

    @staticmethod
    def _attach_tool_output(
        turns: list[NormalizedTurn], call_id: str, output: str
    ) -> None:
        if not call_id or not output:
            return
        for prev in reversed(turns):
            for tc in prev.tool_calls:
                if tc.id == call_id:
                    tc.output = output
                    return

    @staticmethod
    def _extract_token_count(payload: dict[str, Any]) -> int | None:
        in_t = payload.get("input_tokens")
        out_t = payload.get("output_tokens")
        if isinstance(in_t, int) or isinstance(out_t, int):
            return (in_t if isinstance(in_t, int) else 0) + (
                out_t if isinstance(out_t, int) else 0
            )
        return None

    @staticmethod
    def _infer_memory_category(meta: dict[str, Any], body: str) -> MemoryCategory:
        cat = str(meta.get("category") or "").lower()
        if cat in {"preference", "decision", "pitfall", "fact"}:
            return cat  # type: ignore[return-value]
        lower = body.lower()
        if "pitfall" in lower or "avoid" in lower or "locked" in lower:
            return "pitfall"
        if "prefer" in lower:
            return "preference"
        return "fact"


__all__ = ["CodexAdapter"]
