"""Central Markdown memory store (YAML frontmatter) synced with OmnimexDB."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import frontmatter

from omnimex.core.models import (
    DistillStatus,
    MemoryCard,
    MemoryCategory,
    MemoryOrigin,
    normalize_workspace_path,
    utc_now,
)
from omnimex.core.redaction import redact_memory_card
from omnimex.memory.governance import (
    GovernanceReport,
    audit_memories,
    compute_content_hash,
    find_duplicate,
)
from omnimex.memory.native_import import NativeImportSourceStatus, NativeMemoryImporter
from omnimex.storage.db import OmnimexDB, append_workspace_filter
from omnimex.storage.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)

_CATEGORY_DIRS: dict[MemoryCategory, str] = {
    "preference": "preferences",
    "decision": "decisions",
    "pitfall": "pitfalls",
    "fact": "facts",
}

_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 48) -> str:
    slug = _SLUG_CLEAN_RE.sub("-", text.lower()).strip("-")
    if not slug:
        return "memory"
    return slug[:max_len].strip("-") or "memory"


def _parse_iso_dt(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def _format_iso(dt: datetime) -> str:
    utc_dt = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return utc_dt.isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class MemorySyncReport:
    """Summary of disk reconciliation and optional native memory import."""

    synced_files: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    imported_native: int = 0
    native_imports: list[NativeImportSourceStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MemoryManager:
    """Manages Omnimex's central Markdown memory directory and SQLite/FTS5 index."""

    def __init__(
        self,
        db: OmnimexDB,
        memories_dir: str | Path,
        *,
        embedder: EmbeddingProvider | None = None,
        redact: bool = True,
    ) -> None:
        self.db = db
        self.memories_dir = Path(memories_dir).expanduser().resolve()
        self.memories_dir.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._redact = redact

    def _safe_resolve_relative(self, rel_path: str) -> Path:
        candidate = (self.memories_dir / rel_path).resolve()
        try:
            candidate.relative_to(self.memories_dir)
        except ValueError as exc:
            raise ValueError(f"Unsafe memory file path outside central directory: {rel_path}") from exc
        return candidate

    def _target_relative_path(self, card: MemoryCard) -> str:
        if card.file_path:
            # Keep existing path if it's already safe and matches category dir (or imported/)
            norm = card.file_path.replace("\\", "/").lstrip("/")
            self._safe_resolve_relative(norm)
            return norm

        folder = "imported" if card.origin == "native_import" else _CATEGORY_DIRS[card.category]
        slug = _slugify(card.title)
        short_id = re.sub(r"[^a-zA-Z0-9_-]", "", card.id)[-8:] or uuid4().hex[:8]
        candidate = f"{folder}/{slug}.md"
        abs_candidate = self._safe_resolve_relative(candidate)
        if abs_candidate.exists():
            # Check if owned by this same card id; otherwise disambiguate with short_id
            existing = self._read_markdown_file(abs_candidate)
            if existing is not None and existing.id != card.id:
                candidate = f"{folder}/{slug}-{short_id}.md"
        return candidate

    def _write_markdown_file(self, card: MemoryCard) -> MemoryCard:
        rel_path = self._target_relative_path(card)
        abs_path = self._safe_resolve_relative(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        content_hash = compute_content_hash(card.title, card.content, card.category)
        metadata: dict[str, Any] = {
            "id": card.id,
            "title": card.title,
            "category": card.category,
            "workspace": card.workspace,
            "tags": list(card.tags),
            "distill_status": card.distill_status,
            "origin": card.origin,
            "reviewed": bool(card.reviewed),
            "source_session_id": card.source_session_id,
            "source_agent": card.source_agent,
            "source_turn_index": card.source_turn_index,
            "native_source_path": card.native_source_path,
            "confidence": card.confidence,
            "content_hash": content_hash,
            "created_at": _format_iso(card.created_at),
            "updated_at": _format_iso(card.updated_at),
        }
        post = frontmatter.Post(card.content.rstrip() + "\n", **metadata)
        rendered = frontmatter.dumps(post)
        if not rendered.endswith("\n"):
            rendered += "\n"
        abs_path.write_text(rendered, encoding="utf-8")
        return card.model_copy(update={"file_path": rel_path})

    def _read_markdown_file(self, path: Path) -> MemoryCard | None:
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            logger.warning("Failed to read memory markdown %s: %s", path, exc)
            return None

        if not raw.strip():
            return None

        try:
            post = frontmatter.loads(raw)
            meta = dict(post.metadata or {})
            body = (post.content or "").strip()
        except Exception as exc:
            logger.warning("Invalid frontmatter in %s: %s", path, exc)
            return None

        if not body and not meta.get("title"):
            return None

        rel_path = path.resolve().relative_to(self.memories_dir).as_posix()
        parent_folder = path.parent.name.lower()
        inferred_cat: MemoryCategory = {
            "preferences": "preference",
            "decisions": "decision",
            "pitfalls": "pitfall",
            "facts": "fact",
        }.get(parent_folder, "fact")  # type: ignore[assignment]

        raw_cat = str(meta.get("category") or inferred_cat).lower()
        category: MemoryCategory = (
            raw_cat if raw_cat in _CATEGORY_DIRS else inferred_cat  # type: ignore[assignment]
        )

        st_mtime = utc_now()
        with contextlib.suppress(OSError):
            st_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

        raw_status = str(meta.get("distill_status") or "pending")
        distill_status: DistillStatus = (
            raw_status  # type: ignore[assignment]
            if raw_status in ("pending", "heuristic", "llm_distilled")
            else "pending"
        )
        raw_origin = str(meta.get("origin") or "manual")
        origin: MemoryOrigin = (
            raw_origin  # type: ignore[assignment]
            if raw_origin in ("manual", "distilled", "native_import")
            else "manual"
        )

        tags_raw = meta.get("tags")
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []

        fallback_id = f"mem_{hashlib.sha256(rel_path.lower().encode('utf-8')).hexdigest()[:12]}"
        card = MemoryCard(
            id=str(meta.get("id") or fallback_id),
            title=str(meta.get("title") or path.stem).strip(),
            category=category,
            content=body,
            file_path=rel_path,
            tags=tags,
            workspace=normalize_workspace_path(meta.get("workspace")),
            distill_status=distill_status,
            origin=origin,
            reviewed=bool(meta.get("reviewed", origin == "manual")),
            source_session_id=meta.get("source_session_id"),
            source_agent=meta.get("source_agent"),
            source_turn_index=meta.get("source_turn_index"),
            native_source_path=meta.get("native_source_path"),
            confidence=meta.get("confidence"),
            created_at=_parse_iso_dt(meta.get("created_at"), st_mtime),
            updated_at=_parse_iso_dt(meta.get("updated_at"), st_mtime),
        )
        return redact_memory_card(card) if self._redact else card

    def create_memory(
        self,
        *,
        title: str,
        category: MemoryCategory,
        content: str,
        tags: Sequence[str] = (),
        workspace: str | None = None,
        distill_status: DistillStatus = "pending",
        origin: MemoryOrigin = "manual",
        reviewed: bool = True,
        source_session_id: str | None = None,
        source_agent: str | None = None,
        source_turn_index: int | None = None,
        native_source_path: str | None = None,
        confidence: float | None = None,
        id: str | None = None,
        deduplicate: bool = True,
    ) -> MemoryCard:
        """Create and persist a new central Markdown memory card."""
        card = MemoryCard(
            **({"id": id} if id else {}),
            title=title,
            category=category,
            content=content,
            tags=list(tags),
            workspace=workspace,
            distill_status=distill_status,
            origin=origin,
            reviewed=reviewed,
            source_session_id=source_session_id,
            source_agent=source_agent,
            source_turn_index=source_turn_index,
            native_source_path=native_source_path,
            confidence=confidence,
        )
        return self.save_memory(card, deduplicate=deduplicate)

    def save_memory(self, card: MemoryCard, *, deduplicate: bool = True) -> MemoryCard:
        """Scrub secrets, write central Markdown file, and index in OmnimexDB."""
        clean = redact_memory_card(card) if self._redact else card

        if deduplicate:
            existing_all = self.db.list_memories(limit=5000)
            dup = find_duplicate(clean, existing_all)
            if dup is not None:
                merged_tags = sorted(set(dup.tags) | set(clean.tags))
                updated = dup.model_copy(
                    update={
                        "tags": merged_tags,
                        "distill_status": (
                            clean.distill_status
                            if clean.distill_status == "llm_distilled"
                            else dup.distill_status
                        ),
                        "confidence": max(dup.confidence or 0.0, clean.confidence or 0.0)
                        or dup.confidence,
                        "updated_at": utc_now(),
                    }
                )
                persisted = self._write_markdown_file(updated)
                return self.db.upsert_memory(persisted)

        persisted = self._write_markdown_file(clean)
        return self.db.upsert_memory(persisted)

    def get_memory(self, memory_id: str) -> MemoryCard | None:
        return self.db.get_memory(memory_id)

    def list_memories(
        self,
        *,
        category: str | None = None,
        distill_status: str | None = None,
        origin: str | None = None,
        reviewed: bool | None = None,
        workspace: str | None = None,
        agent: str | None = None,
        source_session_id: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryCard]:
        return self.db.list_memories(
            category=category,
            distill_status=distill_status,
            origin=origin,
            reviewed=reviewed,
            workspace=workspace,
            agent=agent,
            source_session_id=source_session_id,
            query=query,
            limit=limit,
            offset=offset,
        )

    def update_memory(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        category: MemoryCategory | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        workspace: str | None = None,
        reviewed: bool | None = None,
        distill_status: DistillStatus | None = None,
    ) -> MemoryCard | None:
        """Update a memory's fields on both Markdown file and SQLite index."""
        existing = self.db.get_memory(memory_id)
        if existing is None:
            return None

        old_rel = existing.file_path
        updates: dict[str, Any] = {"updated_at": utc_now()}
        if title is not None:
            updates["title"] = title
        if category is not None:
            updates["category"] = category
            if existing.origin != "native_import" and old_rel:
                # Re-home into new category subdirectory when category changes
                updates["file_path"] = None
        if content is not None:
            updates["content"] = content
        if tags is not None:
            updates["tags"] = tags
        if workspace is not None:
            updates["workspace"] = workspace
        if reviewed is not None:
            updates["reviewed"] = reviewed
        if distill_status is not None:
            updates["distill_status"] = distill_status

        updated = existing.model_copy(update=updates)
        if self._redact:
            updated = redact_memory_card(updated)
        persisted = self._write_markdown_file(updated)
        if old_rel and persisted.file_path and old_rel != persisted.file_path:
            old_abs = self._safe_resolve_relative(old_rel)
            if old_abs.is_file():
                old_abs.unlink(missing_ok=True)
        return self.db.upsert_memory(persisted)

    def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from SQLite and remove its central Markdown file."""
        existing = self.db.get_memory(memory_id)
        if existing is None:
            return False
        if existing.file_path:
            try:
                abs_path = self._safe_resolve_relative(existing.file_path)
                abs_path.unlink(missing_ok=True)
            except (ValueError, OSError) as exc:
                logger.warning("Could not delete memory file %s: %s", existing.file_path, exc)
        return self.db.delete_memory(memory_id)

    def sync_file(self, file_path: Path) -> MemoryCard | None:
        """Sync a single central ``memories_dir`` Markdown file into SQLite."""
        resolved = file_path.resolve()
        try:
            rel = resolved.relative_to(self.memories_dir).as_posix()
        except ValueError:
            return None

        if not resolved.is_file():
            # File deleted on disk: remove matching DB entry if any
            for card in self.db.list_memories(limit=5000):
                if card.file_path == rel:
                    self.db.delete_memory(card.id)
            return None

        parsed = self._read_markdown_file(resolved)
        if parsed is None:
            return None
        return self.db.upsert_memory(parsed)

    def sync_from_disk(self) -> MemorySyncReport:
        """Reconcile all central ``memories_dir/**/*.md`` files with SQLite."""
        report = MemorySyncReport()
        existing_cards = self.db.list_memories(limit=10000)
        by_id = {c.id: c for c in existing_cards}
        seen_ids: set[str] = set()

        md_files = sorted(self.memories_dir.rglob("*.md"))
        for md_path in md_files:
            card = self._read_markdown_file(md_path)
            if card is None:
                continue
            report.synced_files += 1
            seen_ids.add(card.id)
            if card.id not in by_id:
                report.added += 1
            else:
                prev = by_id[card.id]
                if (
                    prev.title != card.title
                    or prev.content != card.content
                    or prev.category != card.category
                    or prev.reviewed != card.reviewed
                    or prev.tags != card.tags
                ):
                    report.updated += 1
            self.db.upsert_memory(card)

        for prev in existing_cards:
            if prev.id not in seen_ids and prev.file_path:
                self.db.delete_memory(prev.id)
                report.deleted += 1

        return report

    def sync_all(
        self,
        *,
        import_native: bool = True,
        overwrite_existing: bool = False,
        native_importer: NativeMemoryImporter | None = None,
    ) -> MemorySyncReport:
        """Reconcile central Markdown files and optionally import native agent memories."""
        report = self.sync_from_disk()
        if not import_native:
            return report

        importer = native_importer or NativeMemoryImporter()
        native_result = importer.scan()
        report.native_imports = native_result.sources
        report.errors.extend(native_result.errors)

        for card in native_result.cards:
            existing = self.db.get_memory(card.id)
            if existing is not None and not overwrite_existing:
                continue
            # Store read-only imported snapshot inside central data/memories/imported/
            saved = self.save_memory(card, deduplicate=not overwrite_existing)
            if existing is None or saved.id == card.id:
                report.imported_native += 1

        return report

    def search_memories(
        self,
        query: str,
        *,
        category: MemoryCategory | None = None,
        workspace: str | None = None,
        limit: int = 10,
    ) -> list[MemoryCard]:
        """Search central memories via FTS5 BM25 + substring fallback."""
        clean_q = query.strip()
        if not clean_q:
            return self.list_memories(category=category, workspace=workspace, limit=limit)

        tokens = re.findall(r"[A-Za-z0-9_]+", clean_q)
        if not tokens:
            return self.list_memories(category=category, workspace=workspace, limit=limit)

        expr = " OR ".join(f'"{tok}"' for tok in tokens)
        clauses: list[str] = ["memories_fts MATCH ?"]
        params: list[Any] = [expr]
        if category:
            clauses.append("m.category = ?")
            params.append(category)
        append_workspace_filter(clauses, params, "m.workspace_norm", workspace)

        sql = f"""
            SELECT m.*, bm25(memories_fts) AS rank_score
            FROM memories_fts
            JOIN memories m ON m.rowid = memories_fts.rowid
            WHERE {' AND '.join(clauses)}
            ORDER BY rank_score ASC, m.updated_at DESC
            LIMIT ?
        """
        with self.db.lock:
            rows = self.db.connection.execute(sql, [*params, limit]).fetchall()
        results = [self.db._row_to_memory(r) for r in rows]
        if results:
            return results
        return self.list_memories(
            category=category, workspace=workspace, query=clean_q, limit=limit
        )

    def check_governance(self, *, workspace_root: Path | None = None) -> GovernanceReport:
        """Audit central memories for duplicates, conflicts, and stale file refs."""
        cards = self.db.list_memories(limit=5000)
        return audit_memories(cards, workspace_root=workspace_root)


__all__ = [
    "MemoryManager",
    "MemorySyncReport",
]
