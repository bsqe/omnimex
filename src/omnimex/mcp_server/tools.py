"""Progressive, low-token MCP tool functions for Omnimex."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnimex.core.config import OmnimexConfig, load_config
from omnimex.core.models import MemoryCategory, SearchMode, SkillInstallTarget
from omnimex.core.redaction import redact_session
from omnimex.handoff.builder import HandoffBuilder
from omnimex.memory.governance import detect_conflicts
from omnimex.memory.markdown_store import MemoryManager
from omnimex.skill_forge import (
    SkillBuilder,
    install_packaged_skill,
)
from omnimex.skill_forge import (
    save_skill as archive_save_skill,
)
from omnimex.storage.db import OmnimexDB
from omnimex.storage.embeddings import EmbeddingProvider, create_embedder
from omnimex.storage.search import HybridSearcher


def _compact_preview(text: str, max_len: int = 180) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


@dataclass
class OmnimexMcpTools:
    """Container binding progressive MCP tools to Omnimex storage and managers."""

    db: OmnimexDB
    memory_manager: MemoryManager
    searcher: HybridSearcher
    handoff_builder: HandoffBuilder
    skill_builder: SkillBuilder | None = None
    skills_dir: Path | None = None

    @classmethod
    def from_config(
        cls,
        config: OmnimexConfig | None = None,
        *,
        db: OmnimexDB | None = None,
        embedder: EmbeddingProvider | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> OmnimexMcpTools:
        cfg = config or load_config()
        resolved_db = db or OmnimexDB(
            cfg.db_path,
            wal_mode=cfg.storage.wal_mode,
            busy_timeout_ms=cfg.storage.busy_timeout_ms,
            redact=cfg.redaction.enabled,
        )
        resolved_embedder = embedder or create_embedder(cfg.embedding, root_dir=cfg.root_dir)
        resolved_mgr = memory_manager or MemoryManager(
            resolved_db,
            cfg.memories_dir,
            embedder=resolved_embedder,
            redact=cfg.redaction.enabled,
        )
        searcher = HybridSearcher(resolved_db, resolved_embedder)
        handoff = HandoffBuilder(resolved_db, resolved_mgr)
        skill_builder = SkillBuilder(
            resolved_db,
            resolved_mgr,
            distiller_config=cfg.distiller,
        )
        return cls(
            db=resolved_db,
            memory_manager=resolved_mgr,
            searcher=searcher,
            handoff_builder=handoff,
            skill_builder=skill_builder,
            skills_dir=cfg.skills_dir,
        )

    def history_search(
        self,
        query: str,
        agent: str | None = None,
        workspace: str | None = None,
        mode: SearchMode = "hybrid",
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search coding-agent history across all tools, returning compact snippets and IDs."""
        effective_limit = max(1, min(int(limit), 30))
        agents = [a.strip() for a in agent.split(",") if a.strip()] if agent else None
        page = self.searcher.search_page(
            query,
            agents=agents,
            workspace=workspace,
            mode=mode,
            limit=effective_limit,
        )
        return {
            "query": page.query,
            "mode": page.mode,
            "total": page.total,
            "took_ms": page.took_ms,
            "hits": [
                {
                    "turn_id": hit.turn_id,
                    "session_id": hit.session_id,
                    "agent": hit.agent,
                    "session_title": hit.session_title,
                    "workspace": hit.workspace,
                    "turn_index": hit.turn_index,
                    "role": hit.role,
                    "snippet": hit.snippet,
                    "timestamp": hit.timestamp.isoformat().replace("+00:00", "Z")
                    if hit.timestamp
                    else None,
                    "score": round(hit.score, 4),
                }
                for hit in page.hits
            ],
            "hint": "Call history_read(session_id, start_turn=...) to inspect full turns.",
        }

    def history_read(
        self,
        session_id: str,
        start_turn: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Expand a window of redacted turns for a specific session ID."""
        session = self.db.get_session(session_id, include_turns=True)
        if session is None:
            return {"found": False, "error": f"Session not found: {session_id}"}

        clean = redact_session(session)
        start = max(0, int(start_turn))
        window = max(1, min(int(limit), 50))
        sliced = clean.turns[start : start + window]

        turns_out: list[dict[str, Any]] = []
        for t in sliced:
            entry: dict[str, Any] = {
                "turn_index": t.turn_index,
                "role": t.role,
                "content": t.content,
                "timestamp": t.timestamp.isoformat().replace("+00:00", "Z")
                if t.timestamp
                else None,
            }
            if t.thinking:
                entry["thinking"] = _compact_preview(t.thinking, 400)
            if t.tool_calls:
                entry["tool_calls"] = [
                    {
                        "name": tc.name,
                        "status": tc.status,
                        "arguments_preview": _compact_preview(str(tc.arguments), 200),
                        "output_preview": _compact_preview(tc.output, 240) if tc.output else None,
                    }
                    for tc in t.tool_calls
                ]
            turns_out.append(entry)

        return {
            "found": True,
            "session_id": clean.id,
            "agent": clean.agent,
            "title": clean.title,
            "workspace": clean.workspace,
            "total_turns": len(clean.turns),
            "start_turn": start,
            "returned_turns": len(turns_out),
            "has_more": (start + len(turns_out)) < len(clean.turns),
            "turns": turns_out,
        }

    def memory_recall(
        self,
        query: str = "",
        category: MemoryCategory | None = None,
        workspace: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Recall relevant central Markdown memories as compact summaries + IDs."""
        effective_limit = max(1, min(int(limit), 30))
        cards = self.memory_manager.search_memories(
            query,
            category=category,
            workspace=workspace,
            limit=effective_limit,
        )
        return {
            "query": query,
            "count": len(cards),
            "memories": [
                {
                    "id": card.id,
                    "title": card.title,
                    "category": card.category,
                    "workspace": card.workspace,
                    "tags": card.tags,
                    "distill_status": card.distill_status,
                    "origin": card.origin,
                    "reviewed": card.reviewed,
                    "preview": _compact_preview(card.content, 180),
                    "updated_at": card.updated_at.isoformat().replace("+00:00", "Z"),
                }
                for card in cards
            ],
            "hint": "Call memory_read(memory_id) for full Markdown content.",
        }

    def memory_read(self, memory_id: str) -> dict[str, Any]:
        """Read the full central Markdown memory card by ID."""
        card = self.memory_manager.get_memory(memory_id)
        if card is None:
            return {"found": False, "error": f"Memory not found: {memory_id}"}
        return {
            "found": True,
            "memory": card.model_dump(mode="json"),
        }

    def memory_save(
        self,
        title: str,
        category: MemoryCategory,
        content: str,
        workspace: str | None = None,
        tags: Sequence[str] = (),
        source_session_id: str | None = None,
        source_agent: str | None = None,
    ) -> dict[str, Any]:
        """Save a new central Markdown memory under Omnimex's central memories directory."""
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
        if source_agent and "agent-contributed" not in tag_list:
            tag_list.append("agent-contributed")

        existing_all = self.memory_manager.list_memories(limit=1000)
        saved = self.memory_manager.create_memory(
            title=title,
            category=category,
            content=content,
            tags=tag_list,
            workspace=workspace,
            distill_status="heuristic" if source_agent else "pending",
            origin="manual",
            reviewed=True,
            source_session_id=source_session_id,
            source_agent=source_agent,
            deduplicate=True,
        )
        conflicts = detect_conflicts(saved, existing_all)
        return {
            "saved": True,
            "memory": saved.model_dump(mode="json"),
            "conflicts": [
                {
                    "other_id": c.other_id,
                    "reason": c.reason,
                    "similarity": c.similarity,
                }
                for c in conflicts
            ],
        }

    def memory_update(
        self,
        memory_id: str,
        title: str | None = None,
        category: MemoryCategory | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
        workspace: str | None = None,
        reviewed: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing central Markdown memory card."""
        updated = self.memory_manager.update_memory(
            memory_id,
            title=title,
            category=category,
            content=content,
            tags=tags,
            workspace=workspace,
            reviewed=reviewed,
        )
        if updated is None:
            return {"updated": False, "error": f"Memory not found: {memory_id}"}
        return {"updated": True, "memory": updated.model_dump(mode="json")}

    def session_handoff(
        self,
        session_id: str,
        target_agent: str = "generic",
        include_memories: bool = True,
        custom_instructions: str | None = None,
    ) -> dict[str, Any]:
        """Generate a redacted cross-agent HandoffBrief for resuming a session elsewhere."""
        try:
            brief = self.handoff_builder.build(
                session_id,
                target_agent=target_agent,
                include_memories=include_memories,
                custom_instructions=custom_instructions,
            )
        except LookupError as exc:
            return {"found": False, "error": str(exc)}
        return {"found": True, "handoff": brief.model_dump(mode="json")}

    def skill_package(
        self,
        session_ids: Sequence[str] = (),
        memory_ids: Sequence[str] = (),
        name: str | None = None,
        description: str | None = None,
        custom_instructions: str | None = None,
        use_llm: bool = False,
        save: bool = False,
        install_targets: Sequence[SkillInstallTarget] = (),
    ) -> dict[str, Any]:
        """Package selected sessions and/or memories into a reusable SKILL.md skill."""
        builder = self.skill_builder or SkillBuilder(self.db, self.memory_manager)
        try:
            draft = builder.build(
                session_ids=session_ids,
                memory_ids=memory_ids,
                name=name,
                description=description,
                custom_instructions=custom_instructions,
                use_llm=use_llm,
            )
        except (LookupError, ValueError) as exc:
            return {"created": False, "error": str(exc)}

        payload: dict[str, Any] = {
            "created": True,
            "skill": draft.model_dump(mode="json"),
        }
        target_skills_dir = self.skills_dir or (self.memory_manager.memories_dir.parent / "skills")
        if save or install_targets:
            saved = archive_save_skill(target_skills_dir, draft.slug, draft.skill_md)
            payload["saved"] = saved.model_dump(mode="json")
        if install_targets:
            installed = install_packaged_skill(
                draft.slug,
                draft.skill_md,
                targets=install_targets,
            )
            payload["installed"] = installed.model_dump(mode="json")
        return payload


__all__ = ["OmnimexMcpTools"]
