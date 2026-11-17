"""Distillation pipeline orchestrating session extraction and provider backfill."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from omnimex.core.config import DistillerConfig
from omnimex.core.models import MemoryCard, NormalizedSession
from omnimex.distill.base import BaseDistiller
from omnimex.distill.heuristic import HeuristicAndAgentDistiller
from omnimex.distill.ollama import OllamaDistiller
from omnimex.distill.openai_compat import OpenAICompatibleDistiller
from omnimex.memory.markdown_store import MemoryManager
from omnimex.storage.db import OmnimexDB

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DistillSessionResult:
    """Outcome of distilling a single session."""

    session_id: str
    provider: str
    skipped: bool = False
    created_memories: list[MemoryCard] = field(default_factory=list)


@dataclass(slots=True)
class BackfillResult:
    """Outcome of batch/backfill distillation across stored sessions."""

    provider: str
    processed_sessions: int = 0
    created_memories: int = 0
    skipped_sessions: int = 0
    errors: list[str] = field(default_factory=list)


def create_distiller(
    config: DistillerConfig | None = None,
    *,
    http_client: httpx.Client | None = None,
) -> BaseDistiller:
    """Instantiate the configured distiller provider."""
    cfg = config or DistillerConfig()
    if cfg.provider == "ollama":
        return OllamaDistiller(
            model=cfg.model or "qwen2.5:7b-instruct",
            base_url=cfg.base_url or "http://127.0.0.1:11434",
            timeout_seconds=cfg.timeout_seconds,
            client=http_client,
        )
    if cfg.provider == "openai_compatible":
        return OpenAICompatibleDistiller(
            model=cfg.model or "gpt-4o-mini",
            base_url=cfg.base_url or "https://api.openai.com/v1",
            api_key=cfg.api_key,
            timeout_seconds=cfg.timeout_seconds,
            client=http_client,
        )
    return HeuristicAndAgentDistiller()


class DistillationPipeline:
    """Runs session memory extraction, persists central Markdown cards, and tracks backfill."""

    def __init__(
        self,
        db: OmnimexDB,
        memory_manager: MemoryManager,
        distiller: BaseDistiller | None = None,
        *,
        config: DistillerConfig | None = None,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager
        self.config = config or DistillerConfig()
        self.distiller = distiller or create_distiller(self.config)

    def set_distiller(
        self,
        config: DistillerConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> BaseDistiller:
        """Switch the active distiller provider dynamically (e.g., heuristic -> ollama)."""
        self.config = config
        self.distiller = create_distiller(config, http_client=http_client)
        return self.distiller

    def _should_skip(self, session: NormalizedSession, *, force: bool) -> bool:
        if force:
            return False
        if session.distill_status == "llm_distilled":
            return True
        return (
            session.distill_status == "heuristic"
            and self.distiller.target_status == "heuristic"
        )

    def distill_session(
        self,
        session_or_id: NormalizedSession | str,
        *,
        force: bool = False,
    ) -> DistillSessionResult:
        """Distill one session into central Markdown memory cards and update status."""
        if isinstance(session_or_id, str):
            session = self.db.get_session(session_or_id, include_turns=True)
            if session is None:
                raise LookupError(f"Session not found: {session_or_id}")
        else:
            session = session_or_id
            if not session.turns:
                stored = self.db.get_session(session.id, include_turns=True)
                if stored is not None:
                    session = stored

        if self._should_skip(session, force=force):
            return DistillSessionResult(
                session_id=session.id,
                provider=self.distiller.provider_name,
                skipped=True,
            )

        extracted = self.distiller.distill_session(session)
        saved_cards: list[MemoryCard] = []
        for card in extracted:
            saved = self.memory_manager.save_memory(card, deduplicate=True)
            saved_cards.append(saved)

        self.db.mark_session_distill_status(session.id, self.distiller.target_status)
        return DistillSessionResult(
            session_id=session.id,
            provider=self.distiller.provider_name,
            skipped=False,
            created_memories=saved_cards,
        )

    def backfill(
        self,
        *,
        limit: int = 25,
        agent: str | None = None,
        force: bool = False,
        include_heuristic_upgrade: bool = True,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> BackfillResult:
        """Backfill distillation across pending (or heuristic-upgradeable) sessions."""
        agents = [agent] if agent else None
        eligible: list[NormalizedSession] = []
        if force:
            all_sessions, _ = self.db.list_sessions(agents=agents, limit=limit)
            eligible.extend(all_sessions)
        else:
            pending, _ = self.db.list_sessions(
                agents=agents, distill_status="pending", limit=limit
            )
            eligible.extend(pending)
            if (
                len(eligible) < limit
                and include_heuristic_upgrade
                and self.distiller.target_status == "llm_distilled"
            ):
                heuristic_sessions, _ = self.db.list_sessions(
                    agents=agents,
                    distill_status="heuristic",
                    limit=limit - len(eligible),
                )
                eligible.extend(heuristic_sessions)

        result = BackfillResult(provider=self.distiller.provider_name)
        total = len(eligible)
        for idx, meta in enumerate(eligible, start=1):
            try:
                outcome = self.distill_session(meta.id, force=force)
                if outcome.skipped:
                    result.skipped_sessions += 1
                else:
                    result.processed_sessions += 1
                    result.created_memories += len(outcome.created_memories)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Distillation failed for session %s: %s", meta.id, exc)
                result.errors.append(f"{meta.id}: {exc}")
                result.skipped_sessions += 1

            if progress_cb is not None:
                progress_cb(
                    {
                        "processed_sessions": idx,
                        "total_sessions": total,
                        "created_memories": result.created_memories,
                        "provider": self.distiller.provider_name,
                    }
                )

        return result


__all__ = [
    "BackfillResult",
    "DistillSessionResult",
    "DistillationPipeline",
    "create_distiller",
]
