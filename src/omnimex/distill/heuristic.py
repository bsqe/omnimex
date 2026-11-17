"""Zero-cost heuristic memory distiller extracting pitfalls, decisions, preferences, and facts."""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnimex.core.models import (
    MemoryCard,
    MemoryCategory,
    NormalizedSession,
    NormalizedTurn,
)
from omnimex.core.redaction import redact_memory_card, redact_session
from omnimex.distill.base import BaseDistiller
from omnimex.memory.governance import compute_content_hash, extract_file_references

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

_PREFERENCE_RE = re.compile(
    r"\b(always\s+use|never\s+use|prefer\s+.{3,60}\s+over|do\s+not\s+use|"
    r"don't\s+use|must\s+always|default\s+to|convention\s+is)\b",
    re.IGNORECASE,
)

_DECISION_RE = re.compile(
    r"\b(decided\s+to|we\s+will\s+use|switched\s+to|adopted|chose\s+to|"
    r"instead\s+of|architecture|use\s+.{2,40}\s+for\s+|enabled\s+sqlite\s+wal|"
    r"using\s+rrf|reciproca?l\s+rank\s+fusion)\b",
    re.IGNORECASE,
)

_PITFALL_RE = re.compile(
    r"\b(error|exception|traceback|permissionerror|winerror|operationalerror|"
    r"failed|pitfall|gotcha|workaround|blocks\s+in|hangs\s+|avoid\s+|"
    r"deadlock|syntax\s+error|cannot\s+connect)\b",
    re.IGNORECASE,
)

_VERIFY_CMD_RE = re.compile(
    r"\b(pytest|ruff|npm\s+run\s+build|npx\s+tsc|uvicorn|cargo\s+test|go\s+test)\b",
    re.IGNORECASE,
)

_FILE_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "apply_patch",
        "create_file",
        "edit",
        "write",
    }
)


def _clean_sentence(text: str) -> str:
    return " ".join(text.strip().split())


def _short_title(prefix: str, sentence: str, max_len: int = 84) -> str:
    cleaned = sentence.strip(" -*•#`")
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return f"{prefix}: {cleaned}" if prefix and not cleaned.lower().startswith(prefix.lower()) else cleaned


def _iter_sentences(text: str) -> Iterable[str]:
    for part in _SENTENCE_SPLIT_RE.split(text):
        cleaned = _clean_sentence(part)
        if 18 <= len(cleaned) <= 420:
            yield cleaned


def _extract_modified_files_from_turn(turn: NormalizedTurn) -> list[str]:
    found: list[str] = []
    for tc in turn.tool_calls:
        if tc.name.lower() in _FILE_WRITE_TOOLS or "file" in tc.name.lower():
            if isinstance(tc.arguments, dict):
                for key in ("Path", "path", "file_path", "target_file", "TargetFile"):
                    val = tc.arguments.get(key)
                    if isinstance(val, str) and val.strip():
                        found.append(val.replace("\\", "/"))
            elif isinstance(tc.arguments, str):
                found.extend(extract_file_references(tc.arguments))
    found.extend(extract_file_references(turn.content))
    seen: set[str] = set()
    unique: list[str] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


class HeuristicAndAgentDistiller(BaseDistiller):
    """Zero-cost deterministic extractor for errors/fixes, decisions, preferences, and facts."""

    provider_name = "heuristic"
    target_status = "heuristic"

    def __init__(self, *, max_cards_per_session: int = 6) -> None:
        self._max_cards = max(1, max_cards_per_session)

    def distill_session(self, session: NormalizedSession) -> list[MemoryCard]:
        clean_session = redact_session(session)
        candidates: list[MemoryCard] = []
        seen_hashes: set[str] = set()

        def _add_card(
            *,
            title: str,
            category: MemoryCategory,
            content: str,
            turn_index: int | None,
            confidence: float,
            extra_tags: list[str] | None = None,
        ) -> None:
            if len(candidates) >= self._max_cards:
                return
            h = compute_content_hash(title, content, category)
            if h in seen_hashes:
                return
            seen_hashes.add(h)
            tags = sorted({clean_session.agent, category, *(extra_tags or [])})
            card = MemoryCard(
                title=title[:140],
                category=category,
                content=content.strip(),
                tags=tags,
                workspace=clean_session.workspace,
                distill_status="heuristic",
                origin="distilled",
                reviewed=False,
                source_session_id=clean_session.id,
                source_agent=clean_session.agent,
                source_turn_index=turn_index,
                confidence=confidence,
            )
            candidates.append(redact_memory_card(card))

        modified_files: list[str] = []
        verified_commands: list[str] = []

        turns = clean_session.turns
        for idx, turn in enumerate(turns):
            # 1. Tool errors paired with subsequent resolution
            for tc in turn.tool_calls:
                if tc.status == "error" and tc.output:
                    err_snippet = _clean_sentence(tc.output)[:220]
                    fix_snippet = ""
                    if idx + 1 < len(turns) and turns[idx + 1].role == "assistant":
                        fix_snippet = _clean_sentence(turns[idx + 1].content)[:260]
                    body = f"Tool `{tc.name}` failed with: `{err_snippet}`."
                    if fix_snippet:
                        body += f"\n\nResolution: {fix_snippet}"
                    _add_card(
                        title=_short_title("Pitfall", f"{tc.name} failure"),
                        category="pitfall",
                        content=body,
                        turn_index=turn.turn_index,
                        confidence=0.84,
                        extra_tags=["error-fix"],
                    )

                if tc.status == "ok":
                    cmd_str = ""
                    if isinstance(tc.arguments, dict):
                        for k in ("CommandLine", "command", "cmd"):
                            if isinstance(tc.arguments.get(k), str):
                                cmd_str = tc.arguments[k]
                                break
                    elif isinstance(tc.arguments, str):
                        cmd_str = tc.arguments
                    if cmd_str and _VERIFY_CMD_RE.search(cmd_str):
                        verified_commands.append(_clean_sentence(cmd_str)[:180])

            modified_files.extend(_extract_modified_files_from_turn(turn))

            # 2. Explicit preference / decision / pitfall sentences
            for sent in _iter_sentences(turn.content):
                if _PREFERENCE_RE.search(sent):
                    _add_card(
                        title=_short_title("Preference", sent),
                        category="preference",
                        content=sent,
                        turn_index=turn.turn_index,
                        confidence=0.78 if turn.role == "user" else 0.72,
                    )
                elif _PITFALL_RE.search(sent):
                    _add_card(
                        title=_short_title("Pitfall", sent),
                        category="pitfall",
                        content=sent,
                        turn_index=turn.turn_index,
                        confidence=0.76,
                    )
                elif _DECISION_RE.search(sent):
                    _add_card(
                        title=_short_title("Decision", sent),
                        category="decision",
                        content=sent,
                        turn_index=turn.turn_index,
                        confidence=0.74,
                    )

        # 3. Consolidated session fact card (modified files & verified commands)
        unique_files = list(dict.fromkeys(modified_files))
        unique_cmds = list(dict.fromkeys(verified_commands))
        if unique_files or unique_cmds:
            fact_lines: list[str] = []
            if clean_session.title:
                fact_lines.append(f"Session context: {clean_session.title}.")
            if unique_files:
                fact_lines.append("Modified/referenced files: " + ", ".join(f"`{f}`" for f in unique_files[:10]))
            if unique_cmds:
                fact_lines.append("Verified commands: " + "; ".join(f"`{c}`" for c in unique_cmds[:5]))
            _add_card(
                title=f"Session facts: {clean_session.title or clean_session.external_id}"[:100],
                category="fact",
                content="\n\n".join(fact_lines),
                turn_index=turns[-1].turn_index if turns else None,
                confidence=0.72,
                extra_tags=["files" if unique_files else "commands"],
            )

        return candidates


__all__ = ["HeuristicAndAgentDistiller"]
