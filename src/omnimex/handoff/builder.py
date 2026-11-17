"""Cross-agent handoff brief generator aligned with Omnimex API_CONTRACT."""

from __future__ import annotations

import re
from collections.abc import Sequence

from omnimex.core.models import (
    HandoffBrief,
    HandoffMemoryRef,
    HandoffSections,
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
)
from omnimex.core.redaction import redact_handoff_brief, redact_session, redact_text
from omnimex.memory.governance import extract_file_references
from omnimex.memory.markdown_store import MemoryManager
from omnimex.storage.db import OmnimexDB

_AGENT_TARGET_NOTES: dict[str, str] = {
    "antigravity": "Resume in Antigravity with workspace file links and verified command checks.",
    "claude_code": "Resume in Claude Code (`CLAUDE.md` conventions apply; run verification commands before finalizing).",
    "codex": "Resume in Codex CLI: review modified files, apply targeted patches, and re-run test suite.",
    "cursor": "Resume in Cursor Composer/Agent with workspace rules and open file context.",
    "windsurf": "Resume in Windsurf Cascade with workspace memories and listed modified files.",
    "gemini_cli": "Resume in Gemini CLI using the workspace path and verification checks below.",
    "cline": "Resume in Cline task context; inspect modified files before proposing edits.",
    "roo_code": "Resume in Roo Code task mode; preserve architectural decisions and pitfalls below.",
    "copilot": "Resume in GitHub Copilot Chat/CLI with listed modified files in context.",
    "opencode": "Resume in OpenCode session with listed files and constraints.",
    "aider": "Resume in Aider (`/add` the modified files below before editing).",
    "generic": "Resume in target coding agent using the context, decisions, and next steps below.",
}

_TODO_RE = re.compile(
    r"\b(?:todo|next(?:\s+step)?|remaining|still\s+need|follow[- ]?up|pending)\b[:\s-]*(.+)",
    re.IGNORECASE,
)

_DECISION_HINT_RE = re.compile(
    r"\b(?:decided|switched|adopted|chosen|using|enabled|will\s+use|instead\s+of)\b",
    re.IGNORECASE,
)

_PITFALL_HINT_RE = re.compile(
    r"\b(?:error|exception|avoid|never|pitfall|gotcha|warning|workaround|blocked|failed)\b",
    re.IGNORECASE,
)


def _compact(text: str, max_len: int = 240) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def _collect_modified_files(turns: Sequence[NormalizedTurn]) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        norm = raw.strip().replace("\\", "/")
        if norm and norm not in seen:
            seen.add(norm)
            files.append(norm)

    for turn in turns:
        for tc in turn.tool_calls:
            if isinstance(tc.arguments, dict):
                for key in ("Path", "path", "file_path", "target_file", "TargetFile", "file"):
                    val = tc.arguments.get(key)
                    if isinstance(val, str):
                        _add(val)
            arg_text = str(tc.arguments)[:2000]
            out_text = (tc.output or "")[:2000]
            for ref in extract_file_references(arg_text + "\n" + out_text):
                _add(ref)
        for ref in extract_file_references(turn.content[:8000]):
            _add(ref)
    return files[:20]


def _collect_tool_commands(turns: Sequence[NormalizedTurn]) -> list[str]:
    cmds: list[str] = []
    seen: set[str] = set()
    for turn in turns:
        for tc in turn.tool_calls:
            cmd = ""
            if isinstance(tc.arguments, dict):
                for key in ("CommandLine", "command", "cmd"):
                    val = tc.arguments.get(key)
                    if isinstance(val, str) and val.strip():
                        cmd = _compact(val, 140)
                        break
            if cmd:
                entry = f"`{cmd}` ({tc.status})"
                if entry not in seen:
                    seen.add(entry)
                    cmds.append(entry)
            elif tc.name and tc.status == "error":
                err = _compact(tc.output or "error", 120)
                entry = f"Tool `{tc.name}` error: {err}"
                if entry not in seen:
                    seen.add(entry)
                    cmds.append(entry)
    return cmds[:8]


class HandoffBuilder:
    """Builds redacted, target-agent-tailored ``HandoffBrief`` objects from stored sessions."""

    def __init__(
        self,
        db: OmnimexDB,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager

    def build(
        self,
        session_id: str,
        target_agent: str = "generic",
        *,
        include_memories: bool = True,
        include_tool_summary: bool = True,
        max_tokens: int = 1200,
        custom_instructions: str | None = None,
    ) -> HandoffBrief:
        """Build a ``HandoffBrief`` for ``session_id`` tailored to ``target_agent``."""
        session_meta = self.db.get_session(session_id, include_turns=False)
        if session_meta is None:
            raise LookupError(f"Session not found: {session_id}")

        # Cap loaded turns & turn payload sizes so 30+ MB sessions generate handoffs instantly
        tail_turns, total_turns, tail_offset = self.db.get_session_turns_window(
            session_id, limit=80, tail=True
        )
        if tail_offset > 0:
            head_turns, _, _ = self.db.get_session_turns_window(
                session_id, offset=0, limit=min(5, tail_offset), tail=False
            )
            combined_turns = [*head_turns, *tail_turns]
        else:
            combined_turns = tail_turns

        trimmed_turns: list[NormalizedTurn] = []
        for t in combined_turns:
            trimmed_tools = [
                tc.model_copy(
                    update={
                        "arguments": (
                            tc.arguments[:2000]
                            if isinstance(tc.arguments, str) and len(tc.arguments) > 2000
                            else tc.arguments
                        ),
                        "output": tc.output[:2000] if tc.output and len(tc.output) > 2000 else tc.output,
                    }
                )
                for tc in t.tool_calls[:20]
            ]
            trimmed_turns.append(
                t.model_copy(
                    update={
                        "content": t.content[:8000] if len(t.content) > 8000 else t.content,
                        "thinking": t.thinking[:2000] if t.thinking and len(t.thinking) > 2000 else t.thinking,
                        "tool_calls": trimmed_tools,
                    }
                )
            )

        session = session_meta.model_copy(
            update={"turns": trimmed_turns, "turn_count": max(session_meta.turn_count, total_turns)}
        )
        clean_session = redact_session(session)
        relevant_cards = (
            self._select_relevant_memories(clean_session) if include_memories else []
        )

        sections = self._extract_sections(
            clean_session,
            relevant_cards=relevant_cards,
            include_tool_summary=include_tool_summary,
            custom_instructions=custom_instructions,
        )

        title = f"Handoff: {clean_session.title or clean_session.external_id}"
        markdown = self._render_markdown(
            clean_session,
            target_agent=target_agent,
            title=title,
            sections=sections,
            tool_summary=_collect_tool_commands(clean_session.turns) if include_tool_summary else [],
            max_tokens=max_tokens,
        )

        brief = HandoffBrief(
            session_id=clean_session.id,
            source_agent=clean_session.agent,
            target_agent=target_agent,
            workspace=clean_session.workspace,
            title=title,
            markdown=markdown,
            sections=sections,
            token_estimate=max(1, len(markdown) // 4),
            redacted=True,
        )
        return redact_handoff_brief(brief)

    def _select_relevant_memories(self, session: NormalizedSession) -> list[MemoryCard]:
        candidates: list[MemoryCard] = []
        seen: set[str] = set()

        def _add_all(cards: Sequence[MemoryCard]) -> None:
            for c in cards:
                if c.id not in seen:
                    seen.add(c.id)
                    candidates.append(c)

        # 1. Memories distilled directly from this session
        _add_all(self.db.list_memories(source_session_id=session.id, limit=10))
        # 2. Memories matching query / workspace
        if self.memory_manager is not None and session.title:
            _add_all(
                self.memory_manager.search_memories(
                    session.title, workspace=session.workspace, limit=8
                )
            )
        if session.workspace:
            _add_all(self.db.list_memories(workspace=session.workspace, limit=10))
        if not candidates:
            _add_all(self.db.list_memories(limit=6))

        # Prioritize pitfalls & decisions & preferences first
        priority = {"pitfall": 0, "decision": 1, "preference": 2, "fact": 3}
        candidates.sort(key=lambda c: (priority.get(c.category, 4), not c.reviewed))
        return candidates[:8]

    def _extract_sections(
        self,
        session: NormalizedSession,
        *,
        relevant_cards: Sequence[MemoryCard],
        include_tool_summary: bool,
        custom_instructions: str | None,
    ) -> HandoffSections:
        turns = session.turns
        user_turns = [t for t in turns if t.role == "user" and t.content.strip()]
        assistant_turns = [t for t in turns if t.role == "assistant" and t.content.strip()]

        goal = (
            _compact(user_turns[0].content, 280)
            if user_turns
            else (session.title or f"Continue session {session.external_id}")
        )
        if session.summary:
            current_state = _compact(session.summary, 320)
        elif assistant_turns:
            current_state = _compact(assistant_turns[-1].content, 320)
        elif turns:
            current_state = _compact(turns[-1].content or "Session in progress.", 320)
        else:
            current_state = "No turns recorded yet."

        key_decisions: list[str] = []
        pitfalls: list[str] = []
        next_steps: list[str] = []

        for card in relevant_cards:
            if card.category == "decision":
                key_decisions.append(f"{card.title}: {_compact(card.content, 180)}")
            elif card.category in ("pitfall", "preference"):
                pitfalls.append(f"[{card.category}] {card.title}: {_compact(card.content, 180)}")

        for turn in turns:
            for line in turn.content.splitlines():
                stripped = line.strip(" -*•")
                if len(stripped) < 12:
                    continue
                if _TODO_RE.search(stripped):
                    next_steps.append(_compact(stripped, 200))
                elif _PITFALL_HINT_RE.search(stripped) and len(pitfalls) < 6:
                    pitfalls.append(_compact(stripped, 200))
                elif _DECISION_HINT_RE.search(stripped) and len(key_decisions) < 6:
                    key_decisions.append(_compact(stripped, 200))

            if include_tool_summary:
                for tc in turn.tool_calls:
                    if tc.status == "error" and tc.output:
                        pitfalls.append(f"Tool `{tc.name}` error: {_compact(tc.output, 160)}")

        if custom_instructions and custom_instructions.strip():
            next_steps.insert(0, redact_text(_compact(custom_instructions, 260)))

        if not next_steps:
            next_steps.append(
                "Review the modified files and run the workspace verification suite before making further edits."
            )

        modified_files = _collect_modified_files(turns)

        return HandoffSections(
            goal=goal,
            current_state=current_state,
            key_decisions=list(dict.fromkeys(key_decisions))[:6],
            modified_files=modified_files,
            pitfalls_and_constraints=list(dict.fromkeys(pitfalls))[:6],
            next_steps=list(dict.fromkeys(next_steps))[:6],
            relevant_memories=[
                HandoffMemoryRef(id=c.id, title=c.title, category=c.category)
                for c in relevant_cards
            ],
        )

    def _render_markdown(
        self,
        session: NormalizedSession,
        *,
        target_agent: str,
        title: str,
        sections: HandoffSections,
        tool_summary: Sequence[str],
        max_tokens: int,
    ) -> str:
        agent_key = target_agent.strip().lower()
        target_note = _AGENT_TARGET_NOTES.get(agent_key, _AGENT_TARGET_NOTES["generic"])

        lines: list[str] = [
            f"# {title}",
            "",
            f"- **Source Session:** `{session.id}` (`{session.agent}`)",
            f"- **Target Agent:** `{target_agent}`",
        ]
        if session.workspace:
            lines.append(f"- **Workspace:** `{session.workspace}`")
        lines.extend(["", f"> {target_note}", ""])

        lines.extend(["## Goal", sections.goal, ""])
        lines.extend(["## Current State", sections.current_state, ""])

        if sections.key_decisions:
            lines.append("## Key Decisions")
            lines.extend(f"- {item}" for item in sections.key_decisions)
            lines.append("")

        if sections.modified_files:
            lines.append("## Modified / Referenced Files")
            lines.extend(f"- `{p}`" for p in sections.modified_files)
            lines.append("")

        if sections.pitfalls_and_constraints:
            lines.append("## Pitfalls & Constraints")
            lines.extend(f"- {item}" for item in sections.pitfalls_and_constraints)
            lines.append("")

        if tool_summary:
            lines.append("## Recent Verification / Tool Runs")
            lines.extend(f"- {cmd}" for cmd in tool_summary)
            lines.append("")

        if sections.next_steps:
            lines.append("## Next Steps")
            lines.extend(f"- {step}" for step in sections.next_steps)
            lines.append("")

        if sections.relevant_memories:
            lines.append("## Linked Central Memories")
            lines.extend(
                f"- [{m.category}] **{m.title}** (`{m.id}`)"
                for m in sections.relevant_memories
            )
            lines.append("")

        rendered = "\n".join(lines).strip() + "\n"
        max_chars = max(400, max_tokens * 4)
        if len(rendered) > max_chars:
            rendered = rendered[: max_chars - 24].rstrip() + "\n\n... (truncated to token budget)\n"
        return rendered


__all__ = ["HandoffBuilder"]
