"""Read-only bridge for importing native agent memories (Claude Code, Codex, Windsurf)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter

from omnimex.core.models import (
    MemoryCard,
    MemoryCategory,
    normalize_workspace_path,
    utc_now,
)
from omnimex.core.redaction import redact_memory_card

logger = logging.getLogger(__name__)

_CLAUDE_TYPE_MAP: dict[str, MemoryCategory] = {
    "user": "preference",
    "feedback": "pitfall",
    "preference": "preference",
    "project": "fact",
    "reference": "fact",
    "decision": "decision",
    "pitfall": "pitfall",
    "fact": "fact",
}


@dataclass(slots=True)
class NativeImportSourceStatus:
    """Status summary for one native memory source scanned read-only."""

    agent: str
    source_dir: str
    discovered_files: int = 0
    imported_count: int = 0
    last_imported_at: datetime | None = None
    status: str = "not_found"


@dataclass(slots=True)
class NativeImportResult:
    """Aggregated read-only native memory discovery result."""

    cards: list[MemoryCard] = field(default_factory=list)
    sources: list[NativeImportSourceStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _norm_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def _safe_read_text(path: Path, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except PermissionError as exc:
        errors.append(f"permission_denied: {_norm_path(path)} ({exc})")
        return None
    except OSError as exc:
        errors.append(f"read_failed: {_norm_path(path)} ({exc})")
        return None


def _safe_iter_md(root: Path, errors: list[str], *, parent_name: str | None = None) -> list[Path]:
    """Walk ``root`` safely for ``.md`` files without following symlinks or writing."""
    found: list[Path] = []
    try:
        if not root.exists() or not root.is_dir():
            return []
    except OSError as exc:
        errors.append(f"stat_failed: {_norm_path(root)} ({exc})")
        return []

    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except PermissionError as exc:
            errors.append(f"permission_denied: {_norm_path(current)} ({exc})")
            continue
        except OSError as exc:
            errors.append(f"scandir_failed: {_norm_path(current)} ({exc})")
            continue

        for entry in entries:
            p = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(p)
                elif (
                    entry.is_file(follow_symlinks=False)
                    and p.suffix.lower() == ".md"
                    and (parent_name is None or p.parent.name == parent_name)
                ):
                    found.append(p)
            except PermissionError as exc:
                errors.append(f"permission_denied: {_norm_path(p)} ({exc})")
            except OSError:
                continue
    return sorted(found)


def _decode_claude_project_dir(name: str) -> str | None:
    if re.match(r"^[A-Za-z]--", name):
        drive = name[0]
        rest = name[3:].replace("-", "/")
        return normalize_workspace_path(f"{drive}:/{rest}")
    if name.startswith("-"):
        return normalize_workspace_path("/" + name.lstrip("-").replace("-", "/"))
    return None


def _infer_category(meta: dict[str, Any], body: str) -> MemoryCategory:
    raw_cat = str(meta.get("category") or meta.get("type") or "").strip().lower()
    if raw_cat in _CLAUDE_TYPE_MAP:
        return _CLAUDE_TYPE_MAP[raw_cat]
    lower = body.lower()
    if any(k in lower for k in ("prefer", "always ", "style", "convention")):
        return "preference"
    if any(k in lower for k in ("pitfall", "avoid", "error", "never ", "warning", "bug")):
        return "pitfall"
    if any(k in lower for k in ("decided", "architecture", "decision", "chosen")):
        return "decision"
    return "fact"


def _stable_native_id(agent: str, source_path: str) -> str:
    digest = hashlib.sha256(f"{agent}:{source_path.lower()}".encode()).hexdigest()[:12]
    return f"native_{agent}_{digest}"


def _parse_markdown_memory(
    path: Path,
    *,
    agent: str,
    workspace: str | None,
    errors: list[str],
) -> MemoryCard | None:
    raw = _safe_read_text(path, errors)
    if raw is None or not raw.strip():
        return None
    try:
        post = frontmatter.loads(raw)
        meta: dict[str, Any] = dict(post.metadata or {})
        body = (post.content or "").strip()
    except Exception:
        meta = {}
        body = raw.strip()

    if not body:
        return None

    title = str(meta.get("title") or meta.get("name") or path.stem).strip()
    desc = str(meta.get("description") or "").strip()
    if desc and desc not in body:
        body = f"{desc}\n\n{body}"

    category = _infer_category(meta, body)
    tags_raw = meta.get("tags")
    tags: list[str] = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    if agent not in tags:
        tags.append(agent)

    mtime_dt = utc_now()
    with suppress(OSError):
        mtime_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    norm_src = _norm_path(path)
    card = MemoryCard(
        id=_stable_native_id(agent, norm_src),
        title=title or path.stem,
        category=category,
        content=body,
        tags=tags,
        workspace=normalize_workspace_path(meta.get("workspace")) or workspace,
        distill_status="heuristic",
        origin="native_import",
        reviewed=False,
        source_agent=agent,
        native_source_path=norm_src,
        created_at=mtime_dt,
        updated_at=mtime_dt,
    )
    return redact_memory_card(card)


@contextmanager
def _open_sqlite_readonly_temp_copy(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a potentially locked/WAL SQLite database safely via a temp-dir copy."""
    with tempfile.TemporaryDirectory(prefix="omnimex_ro_mem_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        copied_db = tmp_root / db_path.name
        shutil.copy2(db_path, copied_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            try:
                if sidecar.is_file():
                    shutil.copy2(sidecar, tmp_root / sidecar.name)
            except OSError:
                continue

        try:
            conn = sqlite3.connect(copied_db.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            conn = sqlite3.connect(str(copied_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


class NativeMemoryImporter:
    """Strictly read-only scanner for native Claude Code, Codex, and Windsurf memories.

    Never writes, modifies, or deletes files inside any native agent directory.
    """

    def __init__(
        self,
        *,
        claude_roots: Sequence[Path] | None = None,
        codex_roots: Sequence[Path] | None = None,
        windsurf_roots: Sequence[Path] | None = None,
    ) -> None:
        home = Path.home()
        self._claude_roots = list(claude_roots) if claude_roots is not None else [home / ".claude"]
        self._codex_roots = list(codex_roots) if codex_roots is not None else [home / ".codex"]
        self._windsurf_roots = (
            list(windsurf_roots)
            if windsurf_roots is not None
            else [home / ".codeium" / "windsurf"]
        )

    def scan(self) -> NativeImportResult:
        """Read-only scan of all configured native memory directories."""
        result = NativeImportResult()
        now = utc_now()

        # 1. Claude Code: ~/.claude/projects/*/memory/*.md
        claude_files: list[Path] = []
        for root in self._claude_roots:
            projects_dir = root if root.name == "projects" else (root / "projects")
            claude_files.extend(_safe_iter_md(projects_dir, result.errors, parent_name="memory"))

        claude_cards: list[MemoryCard] = []
        for md_path in claude_files:
            ws = _decode_claude_project_dir(md_path.parent.parent.name)
            card = _parse_markdown_memory(
                md_path, agent="claude_code", workspace=ws, errors=result.errors
            )
            if card is not None:
                claude_cards.append(card)
        result.cards.extend(claude_cards)
        result.sources.append(
            NativeImportSourceStatus(
                agent="claude_code",
                source_dir=_norm_path(self._claude_roots[0] / "projects/*/memory/")
                if self._claude_roots
                else "~/.claude/projects/*/memory/",
                discovered_files=len(claude_files),
                imported_count=len(claude_cards),
                last_imported_at=now if claude_cards else None,
                status="synced" if claude_cards else ("not_found" if not claude_files else "empty"),
            )
        )

        # 2. Codex: ~/.codex/memories/*.md (+ read-only memories_1.sqlite if present)
        codex_files: list[Path] = []
        codex_cards: list[MemoryCard] = []
        for root in self._codex_roots:
            mem_dir = root if root.name == "memories" else (root / "memories")
            found = _safe_iter_md(mem_dir, result.errors)
            codex_files.extend(found)
            for md_path in found:
                card = _parse_markdown_memory(
                    md_path, agent="codex", workspace=None, errors=result.errors
                )
                if card is not None:
                    codex_cards.append(card)
            codex_cards.extend(self._scan_codex_sqlite_readonly(root, result.errors))

        result.cards.extend(codex_cards)
        result.sources.append(
            NativeImportSourceStatus(
                agent="codex",
                source_dir=_norm_path(self._codex_roots[0] / "memories/")
                if self._codex_roots
                else "~/.codex/memories/",
                discovered_files=len(codex_files),
                imported_count=len(codex_cards),
                last_imported_at=now if codex_cards else None,
                status="synced" if codex_cards else "not_found",
            )
        )

        # 3. Windsurf: ~/.codeium/windsurf/memories/*.md
        ws_files: list[Path] = []
        ws_cards: list[MemoryCard] = []
        for root in self._windsurf_roots:
            mem_dir = root if root.name == "memories" else (root / "memories")
            found = _safe_iter_md(mem_dir, result.errors)
            ws_files.extend(found)
            for md_path in found:
                card = _parse_markdown_memory(
                    md_path, agent="windsurf", workspace=None, errors=result.errors
                )
                if card is not None:
                    ws_cards.append(card)
        result.cards.extend(ws_cards)
        if ws_files or ws_cards:
            result.sources.append(
                NativeImportSourceStatus(
                    agent="windsurf",
                    source_dir=_norm_path(self._windsurf_roots[0] / "memories/")
                    if self._windsurf_roots
                    else "~/.codeium/windsurf/memories/",
                    discovered_files=len(ws_files),
                    imported_count=len(ws_cards),
                    last_imported_at=now if ws_cards else None,
                    status="synced" if ws_cards else "not_found",
                )
            )

        return result

    @staticmethod
    def _scan_codex_sqlite_readonly(root: Path, errors: list[str]) -> list[MemoryCard]:
        """Read Codex ``memories_1.sqlite`` stage1 outputs strictly read-only via temp copy."""
        cards: list[MemoryCard] = []
        for db_path in (root / "sqlite" / "memories_1.sqlite", root / "memories_1.sqlite"):
            try:
                if not db_path.is_file():
                    continue
            except OSError:
                continue
            try:
                with _open_sqlite_readonly_temp_copy(db_path) as conn:
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage1_outputs'"
                    ).fetchone()
                    if has_table is None:
                        continue
                    rows = conn.execute(
                        "SELECT thread_id, raw_memory, rollout_summary, rollout_slug, generated_at "
                        "FROM stage1_outputs"
                    ).fetchall()
                    for row in rows:
                        tid = str(row["thread_id"] or "")
                        summary = str(row["rollout_summary"] or "").strip()
                        raw_mem = str(row["raw_memory"] or "").strip()
                        content = (
                            f"{summary}\n\n{raw_mem}".strip()
                            if summary and raw_mem
                            else (summary or raw_mem)
                        )
                        if not content:
                            continue
                        title = str(row["rollout_slug"] or tid or "Codex Memory").strip()
                        src = f"{_norm_path(db_path)}#{tid}"
                        card = MemoryCard(
                            id=_stable_native_id("codex_sqlite", src),
                            title=title,
                            category=_infer_category({}, content),
                            content=content,
                            tags=["codex", "stage1"],
                            distill_status="heuristic",
                            origin="native_import",
                            reviewed=False,
                            source_agent="codex",
                            native_source_path=src,
                        )
                        cards.append(redact_memory_card(card))
            except (sqlite3.Error, OSError) as exc:
                errors.append(f"sqlite_read_failed: {_norm_path(db_path)} ({exc})")
        return cards


__all__ = [
    "NativeImportResult",
    "NativeImportSourceStatus",
    "NativeMemoryImporter",
]
