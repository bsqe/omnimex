"""Tests for two-phase FTS-first scanning and priority background vector backfill."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from omnimex.adapters import (
    AdapterRegistry,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
)
from omnimex.core.config import OmnimexConfig
from omnimex.core.models import NormalizedSession, NormalizedTurn
from omnimex.services import IngestService

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_config(tmp_path: Path) -> OmnimexConfig:
    return OmnimexConfig.model_validate(
        {
            "root_dir": tmp_path,
            "storage": {
                "db_path": "data/omnimex.db",
                "memories_dir": "data/memories",
                "wal_mode": True,
                "busy_timeout_ms": 5000,
            },
            "embedding": {
                "provider": "offline_hash",
                "dimension": 384,
                "allow_download": False,
            },
            "distiller": {
                "provider": "heuristic",
                "auto_distill_on_scan": True,
            },
            "adapters": {
                "watch_enabled": False,
                "scan_on_startup": False,
                "enabled": ["antigravity", "claude_code", "codex"],
            },
            "redaction": {"enabled": True},
        }
    )


def test_deferred_scan_indexes_fts_immediately_and_backfills_vectors(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    ag_root = tmp_path / "sources" / "antigravity"
    claude_root = tmp_path / "sources" / "claude"
    codex_root = tmp_path / "sources" / "codex"
    shutil.copytree(FIXTURES / "antigravity", ag_root)
    shutil.copytree(FIXTURES / "claude_code", claude_root)
    shutil.copytree(FIXTURES / "codex", codex_root)

    registry = AdapterRegistry(
        [
            AntigravityAdapter(search_roots=[ag_root]),
            ClaudeCodeAdapter(search_roots=[claude_root]),
            CodexAdapter(search_roots=[codex_root]),
        ]
    )
    service = IngestService(cfg, registry=registry)
    try:
        sub_q, unsub = service.events.subscribe(topics=["scan", "embedding"])
        try:
            res = service.scan(defer_embeddings=True)
            assert res.new_sessions >= 3
            assert res.new_turns >= 6

            # FTS and hybrid search are both usable immediately once Phase 1 completes.
            fts_page = service.searcher.search_page("hybrid search sqlite", mode="fts")
            hybrid_page = service.searcher.search_page("hybrid search sqlite", mode="hybrid")
            assert fts_page.total >= 1
            assert hybrid_page.total >= 1

            # Wait for Phase 2 background vector backfill worker to complete.
            worker = service._embed_thread
            if worker is not None:
                worker.join(timeout=10.0)

            stats = service.get_stats()
            assert stats["index_embeddings_count"] == stats["total_turns"]

            events: list[str] = []
            while not sub_q.empty():
                evt = sub_q.get_nowait()
                if evt is not None:
                    events.append(evt.event)
            assert "scan.completed" in events
            assert "embedding.started" in events
            assert "embedding.completed" in events
        finally:
            unsub()
    finally:
        service.close()


def test_embedding_priority_and_concurrent_deleted_turn_guard(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    service = IngestService(cfg)
    try:
        older_user = NormalizedSession(
            agent="claude_code",
            external_id="older-user",
            title="Older real session",
            source_path="~/.claude/projects/demo/old.jsonl",
            started_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 11, 10, 9, 5, tzinfo=UTC),
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="user",
                    content="Older user prompt",
                    timestamp=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
                )
            ],
        )
        newest_subagent = NormalizedSession(
            agent="antigravity",
            external_id="newest-subagent",
            title="The following is a <SYSTEM_MESSAGE>",
            source_path="~/.gemini/antigravity/brain/sub/transcript.jsonl",
            started_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 11, 20, 9, 5, tzinfo=UTC),
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="system",
                    content="System swarm instruction",
                    timestamp=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
                )
            ],
        )
        newest_user = NormalizedSession(
            agent="codex",
            external_id="newest-user",
            title="Newest real session",
            source_path="~/.codex/sessions/new.jsonl",
            started_at=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 11, 19, 9, 5, tzinfo=UTC),
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="user",
                    content="Latest user question",
                    timestamp=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
                )
            ],
        )

        service.db.upsert_session(older_user)
        service.db.upsert_session(newest_subagent)
        service.db.upsert_session(newest_user)

        ordered = service.db.list_session_ids_needing_embeddings(
            service.embedder.model_name,
            service.embedder.dimension,
        )
        assert ordered == [newest_user.id, older_user.id, newest_subagent.id]

        # Concurrent safety: stale/deleted turn_id in batch must not raise IntegrityError.
        valid_turn_id = newest_user.turns[0].id
        vec = [0.1] * service.embedder.dimension
        written = service.db.upsert_turn_embeddings(
            [
                (valid_turn_id, newest_user.id, service.embedder.model_name, vec),
                ("stale-deleted-turn-id", newest_user.id, service.embedder.model_name, vec),
            ]
        )
        assert written == 1
    finally:
        service.close()
