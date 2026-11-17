"""Adapter for Claude Code JSONL transcripts and native Markdown memories."""

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
    build_native_memory_id,
    make_title,
    normalize_session_timestamps,
    normalize_source_path,
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

_HOOK_TAG_RE = re.compile(
    r"<(?:system-reminder|local-command-\w+|command-\w+)\b[^>]*>[\s\S]*?"
    r"</(?:system-reminder|local-command-\w+|command-\w+)>",
    re.IGNORECASE,
)
_SKIP_ENTRY_TYPES = frozenset(
    {
        "summary",
        "file-history-snapshot",
        "file-history-delta",
        "api-request",
        "api-request-blob",
        "api-request-shape",
        "atis-latch",
        "mode",
        "permission-mode",
        "tools-used",
        "last-prompt",
        "progress",
    }
)
_SKIP_DIR_NAMES = frozenset({"tool-results", "memory", "backups", "cache", "downloads"})
_MEMORY_TYPE_TO_CATEGORY: dict[str, MemoryCategory] = {
    "user": "preference",
    "preference": "preference",
    "feedback": "pitfall",
    "pitfall": "pitfall",
    "project": "decision",
    "decision": "decision",
    "reference": "fact",
    "fact": "fact",
}


class ClaudeCodeAdapter(BaseAgentAdapter):
    """Ingest Claude Code transcripts (``~/.claude/projects/*/*.jsonl``) and memories."""

    agent_id = "claude_code"
    display_name = "Claude Code"
    supports_native_memories = True

    def default_roots(self) -> list[Path]:
        if env_dir := os.getenv("CLAUDE_CONFIG_DIR"):
            return [Path(env_dir).expanduser()]
        if xdg := os.getenv("XDG_CONFIG_HOME"):
            return [Path(xdg).expanduser() / "claude-code"]
        return [Path.home() / ".claude", Path.home() / ".config" / "claude"]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return (
            path.suffix.lower() in {".jsonl", ".json", ".claude"}
            and path.name != "history.jsonl"
            and (".claude/" in norm or "claude-code/" in norm or "/projects/" in norm)
        )

    def _project_roots(self, roots: Sequence[Path]) -> list[Path]:
        project_dirs: list[Path] = []
        for root in roots:
            if safe_is_file(root):
                project_dirs.append(root)
                continue
            if not safe_is_dir(root):
                continue
            candidate = root / "projects"
            if safe_is_dir(candidate):
                project_dirs.append(candidate)
            else:
                project_dirs.append(root)
        return project_dirs

    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        resolved = self.resolve_roots(roots)
        discovered: list[Path] = []
        seen: set[str] = set()

        def _is_session_file(p: Path) -> bool:
            if p.name == "history.jsonl":
                return False
            if "auto-mode-classifier" in p.name:
                return False
            return p.suffix.lower() in {".jsonl", ".json", ".claude"}

        for p_root in self._project_roots(resolved):
            for candidate in safe_walk_files(
                p_root,
                predicate=_is_session_file,
                skip_dir_names=_SKIP_DIR_NAMES,
                errors=self.last_errors,
            ):
                key = normalize_source_path(candidate)
                if key not in seen:
                    seen.add(key)
                    discovered.append(candidate)

        discovered.sort()
        return discovered

    def _load_history_project_map(self, roots: Sequence[Path]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        checked: set[str] = set()
        for root in roots:
            for candidate in (root / "history.jsonl", root.parent / "history.jsonl"):
                key = normalize_source_path(candidate)
                if key in checked or not safe_is_file(candidate):
                    continue
                checked.add(key)
                raw = safe_read_text(candidate, errors=self.last_errors)
                if raw is None:
                    continue
                for line in raw.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    sid = obj.get("sessionId")
                    proj = obj.get("project")
                    if isinstance(sid, str) and isinstance(proj, str) and sid and proj:
                        norm_proj = normalize_workspace_path(proj)
                        if norm_proj:
                            mapping[sid] = norm_proj
        return mapping

    def _roots_for_file(self, path: Path) -> list[Path]:
        if self._explicit_roots is not None:
            return list(self._explicit_roots)
        for parent in path.parents:
            if parent.name == "projects":
                return [parent.parent]
        return [path.parent]

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        raw_text = safe_read_text(path, errors=self.last_errors)
        if raw_text is None:
            return []

        if path.suffix.lower() in {".json", ".claude"}:
            return self._parse_monolithic_json(path, raw_text)

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
        if path.suffix.lower() in {".json", ".claude"}:
            return super().parse_file_incremental(
                path, cursor, start_turn_index=start_turn_index
            )
        return self._parse_jsonl_incremental(
            path,
            cursor,
            self._parse_records,
            start_turn_index=start_turn_index,
        )

    def _parse_monolithic_json(self, path: Path, raw_text: str) -> list[NormalizedSession]:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return []

        external_id = str(data.get("sessionId") or data.get("id") or path.stem)
        session_id = f"{self.agent_id}:{external_id}"
        title = str(data.get("title") or "")
        workspace = normalize_workspace_path(data.get("cwd") or data.get("workspace"))
        turns: list[NormalizedTurn] = []
        for idx, msg in enumerate(data["messages"]):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user").lower()
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if not title and role == "user":
                title = make_title(content, fallback=external_id)
            turns.append(
                NormalizedTurn(
                    session_id=session_id,
                    turn_index=idx,
                    role=role,
                    content=content,
                    timestamp=parse_timestamp(msg.get("timestamp")),
                    model=msg.get("model"),
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

        history_map = self._load_history_project_map(self._roots_for_file(path))
        external_id: str = str(initial_meta.get("sid") or "")
        workspace: str | None = initial_meta.get("ws")
        model: str | None = initial_meta.get("model")
        title: str = str(initial_meta.get("title") or "")
        tags: list[str] = []

        for _, rec in records:
            if not external_id and isinstance(rec.get("sessionId"), str) and rec["sessionId"]:
                external_id = rec["sessionId"]
            if workspace is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                workspace = normalize_workspace_path(rec["cwd"])
            if isinstance(rec.get("gitBranch"), str) and rec["gitBranch"]:
                branch_tag = rec["gitBranch"]
                if branch_tag not in tags:
                    tags.append(branch_tag)

        if not external_id:
            external_id = self._infer_external_id(path)
        if workspace is None and external_id in history_map:
            workspace = history_map[external_id]

        session_id = f"{self.agent_id}:{external_id}"
        turns: list[NormalizedTurn] = []
        turn_index = start_turn_index

        for _, rec in records:
            entry_type = str(rec.get("type") or "").lower()
            if entry_type in _SKIP_ENTRY_TYPES or rec.get("isMeta"):
                continue

            ts = parse_timestamp(rec.get("timestamp"))
            if entry_type == "attachment":
                queued_text = self._extract_queued_prompt(rec)
                if queued_text:
                    if not title:
                        title = make_title(queued_text, fallback=external_id)
                    turns.append(
                        NormalizedTurn(
                            session_id=session_id,
                            turn_index=turn_index,
                            role="user",
                            content=queued_text,
                            timestamp=ts,
                        )
                    )
                    turn_index += 1
                continue

            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            role_raw = str(msg.get("role") or rec.get("role") or entry_type).lower()
            raw_content = msg.get("content") if "content" in msg else rec.get("content")

            if entry_type == "user" or role_raw == "user":
                if self._is_tool_result_blocks(raw_content):
                    tool_output = self._flatten_tool_results(raw_content)
                    if tool_output:
                        turns.append(
                            NormalizedTurn(
                                session_id=session_id,
                                turn_index=turn_index,
                                role="tool",
                                content=tool_output,
                                timestamp=ts,
                            )
                        )
                        turn_index += 1
                    continue

                text = self._clean_user_text(raw_content)
                if not text:
                    continue
                if not title:
                    title = make_title(text, fallback=external_id)
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role="user",
                        content=text,
                        timestamp=ts,
                    )
                )
                turn_index += 1
                continue

            if entry_type == "assistant" or role_raw == "assistant" or entry_type == "message":
                turn_model = msg.get("model") if isinstance(msg.get("model"), str) else None
                if turn_model:
                    model = turn_model
                text, thinking, tool_calls = self._parse_assistant_content(raw_content)
                if not text and not thinking and not tool_calls:
                    continue
                role = "assistant" if role_raw not in {"user", "system", "tool"} else role_raw
                token_count = self._extract_usage_tokens(msg.get("usage"))
                turns.append(
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=turn_index,
                        role=role,
                        content=text,
                        thinking=thinking,
                        tool_calls=tool_calls,
                        timestamp=ts,
                        model=turn_model,
                        token_count=token_count,
                    )
                )
                turn_index += 1

        if not turns:
            return []

        if not title:
            title = (
                Path(workspace).name
                if workspace
                else make_title(turns[0].content, fallback=external_id)
            )
        st = safe_stat(path)
        started_at, updated_at = normalize_session_timestamps(
            turns, fallback_mtime_ns=st.st_mtime_ns if st else None
        )
        return [
            NormalizedSession(
                id=session_id,
                agent=self.agent_id,
                external_id=external_id,
                title=title,
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
        """Discover native Claude Code ``projects/*/memory/*.md`` files."""
        resolved = self.resolve_roots(roots)
        cards: list[MemoryCard] = []
        seen_paths: set[str] = set()

        for p_root in self._project_roots(resolved):
            for md_path in safe_walk_files(
                p_root,
                predicate=lambda p: p.suffix.lower() == ".md" and p.parent.name == "memory",
                errors=self.last_errors,
            ):
                norm = normalize_source_path(md_path)
                if norm in seen_paths:
                    continue
                seen_paths.add(norm)
                raw = safe_read_text(md_path, errors=self.last_errors)
                if raw is None or not raw.strip():
                    continue
                meta, body = parse_yaml_frontmatter(raw)
                if not body:
                    continue

                nested_meta = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
                raw_type = str(nested_meta.get("type") or meta.get("type") or "fact").lower()
                category: MemoryCategory = _MEMORY_TYPE_TO_CATEGORY.get(raw_type, "fact")
                title = str(meta.get("name") or meta.get("title") or md_path.stem).strip()
                desc = str(meta.get("description") or "").strip()
                content = f"{desc}\n\n{body}".strip() if desc and desc not in body else body
                st = safe_stat(md_path)
                updated = parse_timestamp(st.st_mtime if st else None)

                cards.append(
                    MemoryCard(
                        id=build_native_memory_id("claude", norm),
                        title=title,
                        category=category,
                        content=content,
                        tags=[raw_type] if raw_type else [],
                        workspace=self._decode_project_dir_name(md_path.parent.parent.name),
                        distill_status="heuristic",
                        origin="native_import",
                        reviewed=False,
                        source_agent=self.agent_id,
                        native_source_path=norm,
                        created_at=updated or parse_timestamp(1),
                        updated_at=updated or parse_timestamp(1),
                    )
                )
        return cards

    @staticmethod
    def _decode_project_dir_name(name: str) -> str | None:
        if re.match(r"^[A-Za-z]--", name):
            drive = name[0]
            rest = name[3:].replace("-", "/")
            return normalize_workspace_path(f"{drive}:/{rest}")
        return None

    @staticmethod
    def _infer_external_id(path: Path) -> str:
        parts = path.parts
        if "projects" in parts:
            idx = parts.index("projects")
            return "/".join(parts[idx:])
        return path.stem

    @staticmethod
    def _extract_queued_prompt(rec: dict[str, Any]) -> str | None:
        att = rec.get("attachment")
        if not isinstance(att, dict) or att.get("type") != "queued_command":
            return None
        if att.get("commandMode") == "task-notification":
            return None
        origin = att.get("origin")
        if isinstance(origin, dict):
            kind = origin.get("kind")
            if kind not in (None, "human"):
                return None
        prompt = att.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return _HOOK_TAG_RE.sub("", prompt).strip() or None
        return None

    @staticmethod
    def _is_tool_result_blocks(content: Any) -> bool:
        return (
            isinstance(content, list)
            and bool(content)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        )

    @staticmethod
    def _flatten_tool_results(content: Any) -> str:
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content", "")
            if isinstance(inner, str):
                text = inner.strip()
            elif isinstance(inner, list):
                text = "\n".join(
                    str(item.get("text", "")).strip()
                    for item in inner
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ).strip()
            else:
                text = str(inner).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _clean_user_text(content: Any) -> str:
        if isinstance(content, str):
            return _HOOK_TAG_RE.sub("", content).strip()
        if isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, str):
                    chunks.append(_HOOK_TAG_RE.sub("", block).strip())
                elif isinstance(block, dict) and block.get("type") in (None, "text", "input_text"):
                    raw = block.get("text")
                    if isinstance(raw, str):
                        chunks.append(_HOOK_TAG_RE.sub("", raw).strip())
            return "\n".join(c for c in chunks if c)
        return ""

    @staticmethod
    def _parse_assistant_content(content: Any) -> tuple[str, str | None, list[ToolCallRecord]]:
        if isinstance(content, str):
            return content.strip(), None, []
        if not isinstance(content, list):
            return "", None, []

        texts: list[str] = []
        thoughts: list[str] = []
        tools: list[ToolCallRecord] = []

        for idx, block in enumerate(content):
            if isinstance(block, str):
                if block.strip():
                    texts.append(block.strip())
                continue
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").lower()
            if btype in ("", "text", "output_text"):
                val = block.get("text")
                if isinstance(val, str) and val.strip():
                    texts.append(val.strip())
            elif btype == "thinking":
                val = block.get("thinking") or block.get("text")
                if isinstance(val, str) and val.strip():
                    thoughts.append(val.strip())
            elif btype == "tool_use":
                name = str(block.get("name") or "tool")
                tool_input = block.get("input")
                tools.append(
                    ToolCallRecord(
                        id=str(block.get("id") or f"tc_{idx}"),
                        name=name,
                        arguments=tool_input if tool_input is not None else {},
                        status="ok",
                    )
                )

        return "\n".join(texts), ("\n".join(thoughts) if thoughts else None), tools

    @staticmethod
    def _extract_usage_tokens(usage: Any) -> int | None:
        if not isinstance(usage, dict):
            return None
        in_t = usage.get("input_tokens")
        out_t = usage.get("output_tokens")
        if isinstance(in_t, int) or isinstance(out_t, int):
            return (in_t if isinstance(in_t, int) else 0) + (
                out_t if isinstance(out_t, int) else 0
            )
        return None


__all__ = ["ClaudeCodeAdapter"]
