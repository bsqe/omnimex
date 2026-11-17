"""Adapters for Cline (``saoudrizwan.claude-dev``) and Roo Code (``rooveterinaryinc.roo-cline``)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omnimex.adapters.base import (
    BaseAgentAdapter,
    extract_text_blocks,
    ide_user_roots,
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
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
    normalize_workspace_path,
)

_IDE_VARIANTS = ("Code", "Code - Insiders", "VSCodium", "Cursor")


class _VsCodeTaskExtensionAdapter(BaseAgentAdapter):
    """Shared implementation for Cline and Roo Code VS Code task directories."""

    extension_id: str = ""

    def default_roots(self) -> list[Path]:
        return [
            user_dir / "globalStorage" / self.extension_id / "tasks"
            for user_dir in ide_user_roots(_IDE_VARIANTS)
        ]

    def matches_path(self, path: Path) -> bool:
        norm = normalize_source_path(path).lower()
        return self.extension_id.lower() in norm and path.name in {
            "ui_messages.json",
            "api_conversation_history.json",
        }

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
                if root.name in {"ui_messages.json", "api_conversation_history.json"}:
                    _add(root)
                continue
            if not safe_is_dir(root):
                continue

            # Support passing IDE User root, extension root, tasks root, or single task dir
            candidate_task_roots: list[Path] = []
            ext_tasks = root / self.extension_id / "tasks"
            direct_tasks = root / "tasks"
            gs_ext_tasks = root / "globalStorage" / self.extension_id / "tasks"
            if safe_is_dir(gs_ext_tasks):
                candidate_task_roots.append(gs_ext_tasks)
            elif safe_is_dir(ext_tasks):
                candidate_task_roots.append(ext_tasks)
            elif safe_is_dir(direct_tasks):
                candidate_task_roots.append(direct_tasks)
            elif safe_is_file(root / "ui_messages.json") or safe_is_file(
                root / "api_conversation_history.json"
            ):
                self._pick_task_file(root, _add)
                continue
            else:
                candidate_task_roots.append(root)

            for tasks_root in candidate_task_roots:
                for entry in safe_scandir(tasks_root, errors=self.last_errors):
                    if entry.name == "taskHistory.json":
                        continue
                    task_dir = Path(entry.path)
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    self._pick_task_file(task_dir, _add)

        discovered.sort()
        return discovered

    @staticmethod
    def _pick_task_file(task_dir: Path, add_fn: Any) -> None:
        ui = task_dir / "ui_messages.json"
        api = task_dir / "api_conversation_history.json"
        if safe_is_file(ui):
            add_fn(ui)
        elif safe_is_file(api):
            add_fn(api)

    def parse_file(self, path: Path) -> list[NormalizedSession]:
        raw = safe_read_text(path, errors=self.last_errors)
        if raw is None:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list) or not items:
            return []

        task_dir = path.parent
        external_id = task_dir.name
        session_id = f"{self.agent_id}:{external_id}"
        meta = self._load_task_metadata(task_dir)
        title = str(meta.get("title") or "").strip()
        workspace = normalize_workspace_path(
            meta.get("rootPath") or meta.get("cwd") or meta.get("workspace")
        )

        parsed_rows: list[tuple[Any, NormalizedTurn]] = []
        for msg in items:
            if not isinstance(msg, dict):
                continue
            raw_role = str(msg.get("role") or msg.get("type") or "user").lower()
            if raw_role in {"ask", "user", "human"}:
                role = "user"
            elif raw_role in {"say", "assistant", "model", "ai"}:
                role = "assistant"
            elif raw_role in {"system", "tool"}:
                role = raw_role
            else:
                role = "assistant"

            raw_content = (
                msg.get("content")
                if msg.get("content") is not None
                else (msg.get("text") if msg.get("text") is not None else msg.get("message"))
            )
            text, thinking = extract_text_blocks(raw_content)
            tool_calls = self._extract_tool_uses(raw_content)
            if not text and not thinking and not tool_calls:
                continue

            ts = parse_timestamp(
                msg.get("timestamp") or msg.get("created_at") or msg.get("ts")
            )
            parsed_rows.append(
                (
                    ts.timestamp() if ts else float("inf"),
                    NormalizedTurn(
                        session_id=session_id,
                        turn_index=0,
                        role=role,
                        content=text,
                        thinking=thinking,
                        tool_calls=tool_calls,
                        timestamp=ts,
                    ),
                )
            )

        if not parsed_rows:
            return []

        parsed_rows.sort(key=lambda pair: pair[0])
        turns: list[NormalizedTurn] = []
        for idx, (_, turn) in enumerate(parsed_rows):
            turns.append(
                turn.model_copy(update={"turn_index": idx, "id": f"{session_id}#t{idx}"})
            )

        if not title:
            first_user = next((t.content for t in turns if t.role == "user"), turns[0].content)
            title = make_title(first_user, fallback=external_id)

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
                turns=turns,
            )
        ]

    @staticmethod
    def _load_task_metadata(task_dir: Path) -> dict[str, Any]:
        meta_file = task_dir / "task_metadata.json"
        if not safe_is_file(meta_file):
            return {}
        raw = safe_read_text(meta_file)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _extract_tool_uses(content: Any) -> list[ToolCallRecord]:
        if not isinstance(content, list):
            return []
        tools: list[ToolCallRecord] = []
        for idx, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools.append(
                    ToolCallRecord(
                        id=str(block.get("id") or f"tc_{idx}"),
                        name=str(block.get("name") or "tool"),
                        arguments=block.get("input") or {},
                        status="ok",
                    )
                )
        return tools


class ClineAdapter(_VsCodeTaskExtensionAdapter):
    """Ingest Cline VS Code extension tasks."""

    agent_id = "cline"
    display_name = "Cline"
    extension_id = "saoudrizwan.claude-dev"


class RooCodeAdapter(_VsCodeTaskExtensionAdapter):
    """Ingest Roo Code VS Code extension tasks."""

    agent_id = "roo_code"
    display_name = "Roo Code"
    extension_id = "rooveterinaryinc.roo-cline"


__all__ = ["ClineAdapter", "RooCodeAdapter"]
