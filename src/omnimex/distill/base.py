"""Base distiller abstraction and shared prompt/JSON parsing helpers."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from omnimex.core.models import (
    DistillStatus,
    MemoryCard,
    MemoryCategory,
    NormalizedSession,
)
from omnimex.core.redaction import redact_memory_card, redact_session, redact_text

_VALID_CATEGORIES: frozenset[str] = frozenset({"preference", "decision", "pitfall", "fact"})

_DISTILL_SYSTEM_PROMPT = (
    "You extract reusable developer memory cards from coding-agent transcripts. "
    "Return ONLY a JSON array of objects, each with keys: "
    '"title" (short concise string), '
    '"category" (one of "preference", "decision", "pitfall", "fact"), '
    '"content" (concise markdown explanation with exact file paths, commands, or rules), '
    '"tags" (array of short lowercase strings), '
    '"confidence" (float 0.0..1.0), '
    '"source_turn_index" (integer or null). '
    "Never include secrets, tokens, or credentials. Return [] if nothing durable is found."
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def format_session_for_distill(session: NormalizedSession, *, max_chars: int = 14000) -> str:
    """Format a redacted session transcript into a compact prompt string."""
    clean = redact_session(session)
    lines: list[str] = [
        f"Session: {clean.id}",
        f"Agent: {clean.agent}",
        f"Title: {clean.title or '(untitled)'}",
    ]
    if clean.workspace:
        lines.append(f"Workspace: {clean.workspace}")
    lines.append("")

    used = sum(len(line) + 1 for line in lines)
    for turn in clean.turns:
        chunk_parts = [f"[Turn {turn.turn_index} | {turn.role}]"]
        if turn.content:
            chunk_parts.append(turn.content.strip())
        for tc in turn.tool_calls:
            arg_preview = (
                json.dumps(tc.arguments, ensure_ascii=False)
                if isinstance(tc.arguments, dict)
                else str(tc.arguments)
            )
            tc_line = f"- tool {tc.name} ({tc.status}): {arg_preview[:240]}"
            if tc.output:
                tc_line += f" -> {tc.output[:240]}"
            chunk_parts.append(tc_line)
        block = "\n".join(chunk_parts)
        if used + len(block) + 2 > max_chars:
            lines.append("... (truncated)")
            break
        lines.append(block)
        lines.append("")
        used += len(block) + 2

    return redact_text("\n".join(lines).strip())


def parse_llm_memory_cards(
    raw_text: str,
    session: NormalizedSession,
    *,
    distill_status: DistillStatus = "llm_distilled",
) -> list[MemoryCard]:
    """Parse JSON array (or object with ``memories`` key) returned by an LLM into MemoryCards."""
    text = raw_text.strip()
    if not text:
        return []

    if match := _JSON_FENCE_RE.search(text):
        text = match.group(1).strip()

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try locating the outermost JSON array or object
        arr_start, arr_end = text.find("["), text.rfind("]")
        if arr_start != -1 and arr_end > arr_start:
            try:
                parsed = json.loads(text[arr_start : arr_end + 1])
            except json.JSONDecodeError:
                parsed = None

    if isinstance(parsed, dict):
        parsed = parsed.get("memories") or parsed.get("cards") or parsed.get("items") or []
    if not isinstance(parsed, list):
        return []

    cards: list[MemoryCard] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title or not content:
            continue

        raw_cat = str(item.get("category") or "fact").strip().lower()
        category: MemoryCategory = (
            raw_cat if raw_cat in _VALID_CATEGORIES else "fact"  # type: ignore[assignment]
        )

        raw_tags = item.get("tags")
        tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []

        conf_val: float | None = None
        if item.get("confidence") is not None:
            try:
                conf_val = max(0.0, min(1.0, float(item["confidence"])))
            except (TypeError, ValueError):
                conf_val = None

        turn_idx: int | None = None
        if item.get("source_turn_index") is not None:
            try:
                idx = int(item["source_turn_index"])
                if idx >= 0:
                    turn_idx = idx
            except (TypeError, ValueError):
                turn_idx = None

        card = MemoryCard(
            title=title[:140],
            category=category,
            content=content,
            tags=tags,
            workspace=session.workspace,
            distill_status=distill_status,
            origin="distilled",
            reviewed=False,
            source_session_id=session.id,
            source_agent=session.agent,
            source_turn_index=turn_idx,
            confidence=conf_val if conf_val is not None else 0.85,
        )
        cards.append(redact_memory_card(card))

    return cards


class BaseDistiller(ABC):
    """Abstract base class for zero-cost heuristic and LLM-backed memory distillers."""

    provider_name: str = "base"
    target_status: DistillStatus = "heuristic"

    @abstractmethod
    def distill_session(self, session: NormalizedSession) -> list[MemoryCard]:
        """Extract candidate MemoryCards from a normalized session."""


__all__ = [
    "BaseDistiller",
    "_DISTILL_SYSTEM_PROMPT",
    "format_session_for_distill",
    "parse_llm_memory_cards",
]
