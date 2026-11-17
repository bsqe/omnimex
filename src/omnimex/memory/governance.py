"""Memory governance: SHA-256 dedup, conflict detection, and code-staleness checks."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from omnimex.core.models import MemoryCard, normalize_workspace_path

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "using",
        "with",
    }
)

_NEGATION_MARKERS = frozenset(
    {
        "avoid",
        "cannot",
        "disable",
        "disabled",
        "do not",
        "don't",
        "dont",
        "forbid",
        "never",
        "no",
        "not",
        "without",
    }
)

_FILE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:[A-Za-z]:[/\\])?"
    r"(?:[A-Za-z0-9_.-]+[/\\])*"
    r"[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|toml|yaml|yml|json|sql|md|rs|go|css|html)"
    r"(?![A-Za-z0-9_])"
)


@dataclass(slots=True)
class MemoryConflict:
    """Potential semantic or directive conflict between two memory cards."""

    memory_id: str
    other_id: str
    category: str
    reason: str
    similarity: float


@dataclass(slots=True)
class MemoryStalenessResult:
    """Result of checking file references in a memory against workspace mtimes."""

    memory_id: str
    is_stale: bool
    referenced_files: list[str] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GovernanceReport:
    """Summary of duplicate hashes, potential conflicts, and stale memories."""

    duplicate_groups: list[list[str]] = field(default_factory=list)
    conflicts: list[MemoryConflict] = field(default_factory=list)
    stale_memories: list[MemoryStalenessResult] = field(default_factory=list)


def compute_content_hash(
    title: str,
    content: str,
    category: str | None = None,
) -> str:
    """Compute a normalized SHA-256 content hash for memory deduplication."""
    norm_title = " ".join(title.strip().lower().split())
    norm_body = "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").strip().splitlines())
    parts = [category.strip().lower()] if category else []
    parts.extend([norm_title, norm_body])
    payload = "\n---\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tokenize_topic(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


def _has_negation_flip(text_a: str, text_b: str) -> bool:
    lower_a = text_a.lower()
    lower_b = text_b.lower()
    neg_a = any(marker in lower_a for marker in _NEGATION_MARKERS)
    neg_b = any(marker in lower_b for marker in _NEGATION_MARKERS)
    return neg_a != neg_b


def find_duplicate(
    candidate: MemoryCard,
    existing: Sequence[MemoryCard],
) -> MemoryCard | None:
    """Return an existing card with identical normalized SHA-256 content hash."""
    target_hash = compute_content_hash(candidate.title, candidate.content, candidate.category)
    for card in existing:
        if card.id == candidate.id:
            continue
        if compute_content_hash(card.title, card.content, card.category) == target_hash:
            return card
    return None


def _workspaces_compatible(ws_a: str | None, ws_b: str | None) -> bool:
    if not ws_a or not ws_b:
        return True
    norm_a = (normalize_workspace_path(ws_a) or "").lower()
    norm_b = (normalize_workspace_path(ws_b) or "").lower()
    return (
        norm_a == norm_b
        or norm_a.startswith(norm_b.rstrip("/") + "/")
        or norm_b.startswith(norm_a.rstrip("/") + "/")
    )


def detect_conflicts(
    candidate: MemoryCard,
    existing: Sequence[MemoryCard],
    *,
    overlap_threshold: float = 0.45,
) -> list[MemoryConflict]:
    """Identify existing memories in the same category/workspace that may conflict."""
    cand_hash = compute_content_hash(candidate.title, candidate.content, candidate.category)
    cand_tokens = _tokenize_topic(f"{candidate.title} {candidate.content}")
    if not cand_tokens:
        return []

    conflicts: list[MemoryConflict] = []
    for card in existing:
        if card.id == candidate.id or card.category != candidate.category:
            continue
        if not _workspaces_compatible(candidate.workspace, card.workspace):
            continue
        other_hash = compute_content_hash(card.title, card.content, card.category)
        if other_hash == cand_hash:
            continue

        other_tokens = _tokenize_topic(f"{card.title} {card.content}")
        if not other_tokens:
            continue
        union = cand_tokens | other_tokens
        jaccard = len(cand_tokens & other_tokens) / len(union)
        title_overlap = len(
            _tokenize_topic(candidate.title) & _tokenize_topic(card.title)
        )
        polarity_flip = _has_negation_flip(
            f"{candidate.title} {candidate.content}",
            f"{card.title} {card.content}",
        )

        if jaccard >= overlap_threshold or (title_overlap >= 2 and polarity_flip):
            reason = (
                "Opposing polarity on overlapping topic"
                if polarity_flip
                else "Overlapping topic in same category with differing guidance"
            )
            conflicts.append(
                MemoryConflict(
                    memory_id=candidate.id,
                    other_id=card.id,
                    category=candidate.category,
                    reason=reason,
                    similarity=round(jaccard, 3),
                )
            )
    return conflicts


def extract_file_references(content: str) -> list[str]:
    """Extract unique code/config file path references from markdown content."""
    seen: set[str] = set()
    refs: list[str] = []
    for match in _FILE_REF_RE.finditer(content):
        raw = match.group(0).strip("`'\".,;:()[]")
        norm = raw.replace("\\", "/")
        # Skip URL paths or version strings like v1.2.js unless slash/extension valid
        if "://" in norm or norm.startswith("http"):
            continue
        if norm not in seen:
            seen.add(norm)
            refs.append(norm)
    return refs


def check_staleness(
    card: MemoryCard,
    *,
    workspace_root: Path | None = None,
) -> MemoryStalenessResult:
    """Check whether code files referenced by ``card`` changed after ``card.updated_at``."""
    refs = extract_file_references(f"{card.title}\n{card.content}")
    if not refs:
        return MemoryStalenessResult(memory_id=card.id, is_stale=False)

    root: Path | None = workspace_root
    if root is None and card.workspace:
        candidate = Path(card.workspace)
        if candidate.is_dir():
            root = candidate

    if root is None or not root.is_dir():
        return MemoryStalenessResult(
            memory_id=card.id,
            is_stale=False,
            referenced_files=refs,
        )

    mem_updated = card.updated_at
    if mem_updated.tzinfo is None:
        mem_updated = mem_updated.replace(tzinfo=UTC)

    stale_files: list[str] = []
    missing_files: list[str] = []
    reasons: list[str] = []

    for ref in refs:
        ref_path = Path(ref)
        candidates: list[Path] = []
        if ref_path.is_absolute():
            candidates.append(ref_path)
        else:
            candidates.append(root / ref_path)
            if not str(ref).startswith("src/"):
                candidates.append(root / "src" / ref_path)

        matched: Path | None = None
        for cand in candidates:
            try:
                if cand.is_file():
                    matched = cand
                    break
            except OSError:
                continue

        if matched is None:
            # Only flag missing when the reference has a path separator (likely a repo file,
            # not a generic mention like "pyproject.toml" in an unrelated temp workspace).
            if "/" in ref:
                missing_files.append(ref)
                reasons.append(f"Referenced file missing: {ref}")
            continue

        try:
            mtime = datetime.fromtimestamp(matched.stat().st_mtime, tz=UTC)
        except OSError:
            continue

        if mtime > mem_updated:
            stale_files.append(ref)
            reasons.append(
                f"Referenced file modified after memory update: {ref} "
                f"({mtime.isoformat()} > {mem_updated.isoformat()})"
            )

    is_stale = bool(stale_files or missing_files)
    return MemoryStalenessResult(
        memory_id=card.id,
        is_stale=is_stale,
        referenced_files=refs,
        stale_files=stale_files,
        missing_files=missing_files,
        reasons=reasons,
    )


def audit_memories(
    cards: Sequence[MemoryCard],
    *,
    workspace_root: Path | None = None,
) -> GovernanceReport:
    """Run duplicate, conflict, and staleness checks across a collection of memories."""
    by_hash: dict[str, list[str]] = {}
    for card in cards:
        h = compute_content_hash(card.title, card.content, card.category)
        by_hash.setdefault(h, []).append(card.id)
    dup_groups = [ids for ids in by_hash.values() if len(ids) > 1]

    seen_hashes: set[str] = set()
    unique_cards: list[MemoryCard] = []
    for card in cards:
        h = compute_content_hash(card.title, card.content, card.category)
        if h not in seen_hashes:
            seen_hashes.add(h)
            unique_cards.append(card)

    seen_pairs: set[tuple[str, str]] = set()
    conflicts: list[MemoryConflict] = []
    stale_memories: list[MemoryStalenessResult] = []

    for card in unique_cards:
        for conflict in detect_conflicts(card, unique_cards):
            pair = tuple(sorted((conflict.memory_id, conflict.other_id)))
            if pair not in seen_pairs:
                seen_pairs.add((pair[0], pair[1]))
                conflicts.append(conflict)

    for card in cards:
        staleness = check_staleness(card, workspace_root=workspace_root)
        if staleness.is_stale:
            stale_memories.append(staleness)

    return GovernanceReport(
        duplicate_groups=dup_groups,
        conflicts=conflicts,
        stale_memories=stale_memories,
    )


__all__ = [
    "GovernanceReport",
    "MemoryConflict",
    "MemoryStalenessResult",
    "audit_memories",
    "check_staleness",
    "compute_content_hash",
    "detect_conflicts",
    "extract_file_references",
    "find_duplicate",
]
