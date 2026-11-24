"""Synthesize reusable, redacted SKILL.md bundles from sessions and central memories."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence

import httpx

from omnimex.core.config import DistillerConfig
from omnimex.core.models import (
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    SkillDraft,
)
from omnimex.core.redaction import redact_memory_card, redact_session, redact_text
from omnimex.handoff.builder import (
    _DECISION_HINT_RE,
    _PITFALL_HINT_RE,
    _collect_modified_files,
    _collect_tool_commands,
    _compact,
)
from omnimex.memory.markdown_store import MemoryManager
from omnimex.storage.db import OmnimexDB

logger = logging.getLogger(__name__)

_ASCII_SLUG_RE = re.compile(r"[a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n?(.*)$")
_SKILL_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\r?\n([\s\S]*?)\r?\n```\s*$", re.IGNORECASE)

_SKILL_SYSTEM_PROMPT = (
    "You convert coding-agent engineering sessions and distilled memory cards into a clean, "
    "reusable Agent Skill Markdown file (`SKILL.md`). Return ONLY valid Markdown beginning with "
    "YAML frontmatter (`---`, `name: <slug>`, `description: ...`, `---`) followed by concise "
    "sections: `# Title`, `## When to Use`, `## Step-by-Step Workflow`, "
    "`## Architectural Decisions & Preferences`, `## Pitfalls & Guardrails`, and "
    "`## Verification & Reference Files`. Never include API keys, passwords, tokens, or personal data."
)


def slugify_skill_name(
    raw_name: str | None,
    *,
    fallback_tags: Sequence[str] = (),
    seed: str = "",
) -> str:
    """Convert an arbitrary title or name into a safe kebab-case Skill folder slug."""
    candidates = [raw_name or "", " ".join(fallback_tags)]
    for candidate in candidates:
        tokens = _ASCII_SLUG_RE.findall(candidate.lower())
        if tokens:
            slug = "-".join(tokens)[:56].strip("-")
            if slug:
                return slug

    digest_source = (raw_name or seed or "omnimex-skill").encode("utf-8", errors="ignore")
    short_hash = hashlib.sha256(digest_source).hexdigest()[:8]
    return f"omnimex-skill-{short_hash}"


def format_skill_frontmatter(slug: str, description: str) -> str:
    """Format safe single-line YAML frontmatter for ``SKILL.md``."""
    clean_slug = slugify_skill_name(slug)
    clean_desc = " ".join((description or "Reusable engineering workflow skill.").splitlines()).strip()
    escaped_desc = clean_desc.replace("\\", "\\\\").replace('"', '\\"')
    return f'---\nname: {clean_slug}\ndescription: "{escaped_desc}"\n---\n'


def parse_skill_frontmatter(skill_md: str) -> tuple[str | None, str, str]:
    """Extract ``(name, description, body)`` from a ``SKILL.md`` string."""
    match = _FRONTMATTER_RE.match(skill_md.strip())
    if not match:
        return None, "", skill_md.strip()

    yaml_block, body = match.group(1), match.group(2).strip()
    parsed_name: str | None = None
    parsed_desc = ""
    for line in yaml_block.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key_clean = key.strip().lower()
        val_clean = val.strip()
        if (val_clean.startswith('"') and val_clean.endswith('"')) or (
            val_clean.startswith("'") and val_clean.endswith("'")
        ):
            val_clean = val_clean[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if key_clean == "name" and val_clean:
            parsed_name = slugify_skill_name(val_clean)
        elif key_clean == "description":
            parsed_desc = val_clean
    return parsed_name, parsed_desc, body


def _load_bounded_session(db: OmnimexDB, session_id: str) -> NormalizedSession:
    meta = db.get_session(session_id, include_turns=False)
    if meta is None:
        raise LookupError(f"Session not found: {session_id}")

    tail_turns, total_turns, tail_offset = db.get_session_turns_window(
        session_id, limit=60, tail=True
    )
    if tail_offset > 0:
        head_turns, _, _ = db.get_session_turns_window(
            session_id, offset=0, limit=min(5, tail_offset), tail=False
        )
        combined = [*head_turns, *tail_turns]
    else:
        combined = tail_turns

    trimmed: list[NormalizedTurn] = []
    for turn in combined:
        trimmed_tools = [
            tc.model_copy(
                update={
                    "arguments": (
                        tc.arguments[:1500]
                        if isinstance(tc.arguments, str) and len(tc.arguments) > 1500
                        else tc.arguments
                    ),
                    "output": tc.output[:1500] if tc.output and len(tc.output) > 1500 else tc.output,
                }
            )
            for tc in turn.tool_calls[:15]
        ]
        trimmed.append(
            turn.model_copy(
                update={
                    "content": turn.content[:6000] if len(turn.content) > 6000 else turn.content,
                    "thinking": (
                        turn.thinking[:1500]
                        if turn.thinking and len(turn.thinking) > 1500
                        else turn.thinking
                    ),
                    "tool_calls": trimmed_tools,
                }
            )
        )

    session = meta.model_copy(
        update={"turns": trimmed, "turn_count": max(meta.turn_count, total_turns)}
    )
    return redact_session(session)


def _polish_with_distiller_llm(
    config: DistillerConfig,
    prompt: str,
    *,
    http_client: httpx.Client | None = None,
) -> str | None:
    if config.provider not in {"ollama", "openai_compatible"}:
        return None

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=config.timeout_seconds)
    try:
        if config.provider == "ollama" and not (config.base_url or "").rstrip("/").endswith("/v1"):
            base = (config.base_url or "http://127.0.0.1:11434").rstrip("/")
            resp = client.post(
                f"{base}/api/chat",
                json={
                    "model": config.model or "qwen2.5:7b-instruct",
                    "stream": False,
                    "options": {"temperature": 0.2},
                    "messages": [
                        {"role": "system", "content": _SKILL_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = str(data.get("message", {}).get("content") or "").strip()
        else:
            default_base = (
                "http://127.0.0.1:11434/v1"
                if config.provider == "ollama"
                else "https://api.openai.com/v1"
            )
            base = (config.base_url or default_base).rstrip("/")
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if config.api_key and config.api_key.strip():
                headers["Authorization"] = f"Bearer {config.api_key.strip()}"
            resp = client.post(
                url,
                headers=headers,
                json={
                    "model": config.model or ("qwen2.5:7b-instruct" if config.provider == "ollama" else "gpt-4o-mini"),
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": _SKILL_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            text = str(choices[0].get("message", {}).get("content") or "").strip() if choices else ""

        if not text:
            return None
        if fence_match := _SKILL_FENCE_RE.match(text):
            text = fence_match.group(1).strip()
        return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill LLM polish skipped, using deterministic draft (%s)", exc)
        return None
    finally:
        if owns_client:
            client.close()


class SkillBuilder:
    """Builds redacted, reusable ``SkillDraft`` objects from sessions and central memories."""

    def __init__(
        self,
        db: OmnimexDB,
        memory_manager: MemoryManager | None = None,
        *,
        distiller_config: DistillerConfig | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager
        self.distiller_config = distiller_config or DistillerConfig()
        self._http_client = http_client

    def set_distiller_config(self, config: DistillerConfig) -> None:
        self.distiller_config = config

    def build(
        self,
        *,
        session_ids: Sequence[str] = (),
        memory_ids: Sequence[str] = (),
        name: str | None = None,
        description: str | None = None,
        custom_instructions: str | None = None,
        use_llm: bool = False,
    ) -> SkillDraft:
        """Synthesize a redacted ``SKILL.md`` draft from selected sessions and/or memories."""
        clean_session_ids = list(dict.fromkeys(s.strip() for s in session_ids if s and s.strip()))
        clean_memory_ids = list(dict.fromkeys(m.strip() for m in memory_ids if m and m.strip()))
        if not clean_session_ids and not clean_memory_ids:
            raise ValueError("Select at least one session or memory card to package a Skill.")

        sessions = [_load_bounded_session(self.db, sid) for sid in clean_session_ids]
        memories = self._load_memories(clean_memory_ids, sessions)

        tags = self._collect_tags(sessions, memories)
        title = self._derive_title(name, sessions, memories)
        slug = slugify_skill_name(
            name or title,
            fallback_tags=tags,
            seed="|".join([*clean_session_ids, *clean_memory_ids]),
        )
        workspace = next(
            (s.workspace for s in sessions if s.workspace),
            next((m.workspace for m in memories if m.workspace), None),
        )

        trigger_desc = redact_text(
            _compact(
                description or self._derive_description(title, sessions, memories, tags),
                320,
            )
        )
        heuristic_md = self._render_heuristic_skill_md(
            slug=slug,
            title=title,
            description=trigger_desc,
            sessions=sessions,
            memories=memories,
            tags=tags,
            workspace=workspace,
            custom_instructions=custom_instructions,
        )

        provider = "heuristic"
        final_md = heuristic_md
        if use_llm and self.distiller_config.provider in {"ollama", "openai_compatible"}:
            prompt = (
                f"Target skill slug: `{slug}`\n"
                f"Target trigger description: {trigger_desc}\n"
                + (
                    f"Custom author instructions: {redact_text(custom_instructions.strip())}\n"
                    if custom_instructions and custom_instructions.strip()
                    else ""
                )
                + "\nRewrite and sharpen this draft SKILL.md into a crisp, reusable SOP while "
                "keeping exact file paths, commands, pitfalls, and valid YAML frontmatter:\n\n"
                f"{heuristic_md}"
            )
            polished = _polish_with_distiller_llm(
                self.distiller_config,
                prompt,
                http_client=self._http_client,
            )
            if polished:
                _, _, polished_body = parse_skill_frontmatter(polished)
                final_md = format_skill_frontmatter(slug, trigger_desc) + "\n" + polished_body + "\n"
                provider = self.distiller_config.provider

        final_md = redact_text(final_md).rstrip() + "\n"
        return SkillDraft(
            slug=slug,
            title=redact_text(title),
            description=trigger_desc,
            skill_md=final_md,
            source_session_ids=[s.id for s in sessions],
            source_memory_ids=[m.id for m in memories],
            tags=tags,
            workspace=workspace,
            provider=provider,
            redacted=True,
            token_estimate=max(1, len(final_md) // 4),
        )

    def _load_memories(
        self,
        explicit_ids: Sequence[str],
        sessions: Sequence[NormalizedSession],
    ) -> list[MemoryCard]:
        selected: list[MemoryCard] = []
        seen: set[str] = set()

        def _add(card: MemoryCard) -> None:
            if card.id not in seen:
                seen.add(card.id)
                selected.append(redact_memory_card(card))

        for mid in explicit_ids:
            card = (
                self.memory_manager.get_memory(mid)
                if self.memory_manager is not None
                else self.db.get_memory(mid)
            )
            if card is None:
                raise LookupError(f"Memory not found: {mid}")
            _add(card)

        if sessions:
            for sess in sessions:
                for linked in self.db.list_memories(source_session_id=sess.id, limit=8):
                    _add(linked)
            if not explicit_ids and len(selected) < 6:
                ws = next((s.workspace for s in sessions if s.workspace), None)
                if ws:
                    for card in self.db.list_memories(workspace=ws, limit=6):
                        _add(card)

        priority = {"pitfall": 0, "decision": 1, "preference": 2, "fact": 3}
        selected.sort(key=lambda c: (priority.get(c.category, 4), not c.reviewed))
        return selected[:16]

    @staticmethod
    def _collect_tags(
        sessions: Sequence[NormalizedSession],
        memories: Sequence[MemoryCard],
    ) -> list[str]:
        tags: list[str] = []
        seen: set[str] = set()
        for item in (*memories, *sessions):
            for tag in item.tags:
                clean = str(tag).strip().lower()
                if clean and clean not in seen:
                    seen.add(clean)
                    tags.append(clean)
        return tags[:10]

    @staticmethod
    def _derive_title(
        explicit_name: str | None,
        sessions: Sequence[NormalizedSession],
        memories: Sequence[MemoryCard],
    ) -> str:
        if explicit_name and explicit_name.strip():
            return _compact(explicit_name.strip(), 100)
        if sessions and sessions[0].title:
            return _compact(sessions[0].title, 100)
        if memories:
            return _compact(memories[0].title, 100)
        return "Omnimex Packaged Skill"

    @staticmethod
    def _derive_description(
        title: str,
        sessions: Sequence[NormalizedSession],
        memories: Sequence[MemoryCard],
        tags: Sequence[str],
    ) -> str:
        parts = [f"Reusable workflow and guardrails for {title}."]
        if tags:
            parts.append(f"Use when working with {', '.join(tags[:5])}.")
        elif sessions:
            agents = sorted({s.agent for s in sessions})
            parts.append(f"Use when repeating tasks distilled from {', '.join(agents)} sessions.")
        if memories:
            parts.append(f"Encodes {len(memories)} curated decisions, pitfalls, and facts.")
        return " ".join(parts)

    def _render_heuristic_skill_md(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        sessions: Sequence[NormalizedSession],
        memories: Sequence[MemoryCard],
        tags: Sequence[str],
        workspace: str | None,
        custom_instructions: str | None,
    ) -> str:
        all_turns = [t for sess in sessions for t in sess.turns]
        decisions: list[str] = []
        preferences: list[str] = []
        pitfalls: list[str] = []
        facts: list[str] = []
        workflow_steps: list[str] = []

        for card in memories:
            summary = f"**{card.title}**: {_compact(card.content, 240)}"
            if card.category == "decision":
                decisions.append(summary)
            elif card.category == "preference":
                preferences.append(summary)
            elif card.category == "pitfall":
                pitfalls.append(summary)
            else:
                facts.append(summary)

        for sess in sessions:
            user_turns = [t for t in sess.turns if t.role == "user" and t.content.strip()]
            assistant_turns = [t for t in sess.turns if t.role == "assistant" and t.content.strip()]
            if user_turns:
                workflow_steps.append(f"Clarify scope: {_compact(user_turns[0].content, 180)}")
            if assistant_turns:
                for line in assistant_turns[-1].content.splitlines():
                    cleaned = line.strip(" -*•0123456789.)")
                    if len(cleaned) >= 14 and len(workflow_steps) < 8:
                        workflow_steps.append(_compact(cleaned, 200))
            for turn in sess.turns:
                for line in turn.content.splitlines():
                    stripped = line.strip(" -*•")
                    if len(stripped) < 12:
                        continue
                    if _PITFALL_HINT_RE.search(stripped) and len(pitfalls) < 8:
                        pitfalls.append(_compact(stripped, 200))
                    elif _DECISION_HINT_RE.search(stripped) and len(decisions) < 8:
                        decisions.append(_compact(stripped, 200))
                for tc in turn.tool_calls:
                    if tc.status == "error" and tc.output and len(pitfalls) < 8:
                        pitfalls.append(f"Tool `{tc.name}` error seen: {_compact(tc.output, 160)}")

        if custom_instructions and custom_instructions.strip():
            workflow_steps.insert(0, _compact(custom_instructions.strip(), 240))

        if not workflow_steps:
            for item in [*decisions, *facts]:
                if len(workflow_steps) < 5:
                    workflow_steps.append(item)
        if not workflow_steps:
            workflow_steps.append("Inspect referenced files, follow the constraints below, and verify outputs before finishing.")

        files = _collect_modified_files(all_turns)
        commands = _collect_tool_commands(all_turns)

        lines: list[str] = [
            format_skill_frontmatter(slug, description).rstrip(),
            "",
            f"# {title}",
            "",
            "## When to Use",
            f"- {description}",
        ]
        if workspace:
            lines.append(f"- Primary workspace context: `{workspace}`")
        if tags:
            lines.append(f"- Relevant topics: {', '.join(f'`{t}`' for t in tags)}")
        lines.append("")

        lines.append("## Step-by-Step Workflow")
        for idx, step in enumerate(list(dict.fromkeys(workflow_steps))[:8], start=1):
            lines.append(f"{idx}. {step}")
        lines.append("")

        if decisions or preferences:
            lines.append("## Architectural Decisions & Preferences")
            for item in list(dict.fromkeys([*preferences, *decisions]))[:8]:
                lines.append(f"- {item}")
            lines.append("")

        if pitfalls:
            lines.append("## Pitfalls & Guardrails")
            for item in list(dict.fromkeys(pitfalls))[:8]:
                lines.append(f"- {item}")
            lines.append("")

        if facts or files or commands:
            lines.append("## Reference Files, Commands & Verification")
            for fact in list(dict.fromkeys(facts))[:5]:
                lines.append(f"- {fact}")
            for file_path in files[:10]:
                lines.append(f"- File: `{file_path}`")
            for cmd in commands[:8]:
                lines.append(f"- Verification command: {cmd}")
            lines.append("")

        return redact_text("\n".join(lines).strip() + "\n")


__all__ = [
    "SkillBuilder",
    "format_skill_frontmatter",
    "parse_skill_frontmatter",
    "slugify_skill_name",
]
