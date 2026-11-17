"""Tests for Omnimex central Markdown memory, native import, governance, distillers, and handoff."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter
import httpx
import pytest

from omnimex.core.config import DistillerConfig
from omnimex.core.models import (
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
)
from omnimex.distill import (
    DistillationPipeline,
    HeuristicAndAgentDistiller,
    OllamaDistiller,
    OpenAICompatibleDistiller,
)
from omnimex.handoff import HandoffBuilder
from omnimex.memory import (
    CentralMemoryWatcher,
    MemoryManager,
    NativeMemoryImporter,
    audit_memories,
    check_staleness,
    compute_content_hash,
    detect_conflicts,
)
from omnimex.storage.db import OmnimexDB


def _make_rich_session(
    *,
    agent: str = "antigravity",
    external_id: str = "s-rich-1",
    workspace: str = r"E:\projects\demo",
) -> NormalizedSession:
    t0 = datetime(2026, 11, 17, 7, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 17, 7, 5, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 17, 7, 10, 0, tzinfo=UTC)
    return NormalizedSession(
        agent=agent,
        external_id=external_id,
        title="Migrate search index to SQLite WAL + FTS5",
        workspace=workspace,
        source_path=f"~/.gemini/{agent}/session.jsonl",
        started_at=t0,
        updated_at=t2,
        turns=[
            NormalizedTurn(
                turn_index=0,
                role="user",
                content=(
                    "Always use npm instead of pnpm on Windows. "
                    "Please configure SQLite WAL mode with FTS5 hybrid search."
                ),
                timestamp=t0,
            ),
            NormalizedTurn(
                turn_index=1,
                role="assistant",
                content="Running initial test with token sk-secret-should-be-scrubbed-123456.",
                tool_calls=[
                    ToolCallRecord(
                        id="tc_err",
                        name="run_command",
                        arguments={"CommandLine": "pytest tests/test_core_storage.py"},
                        output="OperationalError: fts5 syntax error near ':'",
                        status="error",
                    ),
                    ToolCallRecord(
                        id="tc_edit",
                        name="edit_file",
                        arguments={"Path": "src/omnimex/storage/search.py"},
                        output="Updated query sanitizer.",
                        status="ok",
                    ),
                ],
                timestamp=t1,
            ),
            NormalizedTurn(
                turn_index=2,
                role="assistant",
                content=(
                    "We decided to sanitize FTS5 tokens by wrapping each word in double quotes. "
                    "Next step: wire FastAPI endpoints and verify frontend build."
                ),
                tool_calls=[
                    ToolCallRecord(
                        id="tc_ok",
                        name="run_command",
                        arguments={"CommandLine": "pytest tests/test_core_storage.py -v"},
                        output="7 passed",
                        status="ok",
                    )
                ],
                timestamp=t2,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Central Markdown Store + Watcher
# ---------------------------------------------------------------------------


def test_markdown_store_crud_frontmatter_sync_and_security(tmp_path: Path) -> None:
    memories_dir = tmp_path / "memories"
    with OmnimexDB(tmp_path / "omnimex.db") as db:
        mgr = MemoryManager(db, memories_dir)

        created = mgr.create_memory(
            title="Use SQLite WAL on Windows",
            category="pitfall",
            content="Avoid lock errors with sk-secret1234567890abcdef removed.",
            tags=["sqlite", "windows"],
            workspace=r"E:\projects\demo",
            distill_status="heuristic",
            origin="distilled",
            reviewed=False,
            source_session_id="antigravity:s1",
            source_agent="antigravity",
            source_turn_index=1,
        )
        assert created.file_path is not None
        assert created.file_path.startswith("pitfalls/")
        md_file = memories_dir / created.file_path
        assert md_file.is_file()
        assert "sk-secret1234567890abcdef" not in md_file.read_text(encoding="utf-8")

        parsed = frontmatter.loads(md_file.read_text(encoding="utf-8"))
        for key in (
            "id",
            "title",
            "category",
            "workspace",
            "tags",
            "distill_status",
            "origin",
            "reviewed",
            "source_session_id",
            "source_agent",
            "source_turn_index",
            "content_hash",
            "created_at",
            "updated_at",
        ):
            assert key in parsed.metadata

        # Category change moves the backing Markdown file cleanly
        updated = mgr.update_memory(
            created.id,
            category="decision",
            reviewed=True,
            distill_status="llm_distilled",
        )
        assert updated is not None
        assert updated.file_path is not None
        assert updated.file_path.startswith("decisions/")
        assert not md_file.exists()
        assert (memories_dir / updated.file_path).is_file()

        # FTS5 search on central memories
        hits = mgr.search_memories("SQLite WAL", category="decision")
        assert len(hits) == 1
        assert hits[0].id == created.id

        # Hand-written markdown on disk syncs into SQLite; deleted file removes from DB
        manual_file = memories_dir / "preferences" / "manual-pref.md"
        manual_file.parent.mkdir(parents=True, exist_ok=True)
        manual_file.write_text(
            "---\ntitle: Prefer TypeScript strict mode\ncategory: preference\ntags: [ts]\n---\n\nAlways enable strictNullChecks.\n",
            encoding="utf-8",
        )
        sync_report = mgr.sync_from_disk()
        assert sync_report.synced_files == 2
        assert sync_report.added == 1

        # Second sync on hand-written file without explicit ID must be idempotent
        second_sync = mgr.sync_from_disk()
        assert second_sync.synced_files == 2
        assert second_sync.added == 0
        assert second_sync.updated == 0
        assert second_sync.deleted == 0

        manual_file.unlink()
        sync_after_delete = mgr.sync_from_disk()
        assert sync_after_delete.deleted == 1

        # Path traversal guard
        with pytest.raises(ValueError):
            mgr.save_memory(
                MemoryCard(
                    title="Escape",
                    category="fact",
                    content="bad",
                    file_path="../../outside.md",
                )
            )

        assert mgr.delete_memory(created.id) is True
        assert mgr.get_memory(created.id) is None


def test_central_memory_watcher_syncs_edits(tmp_path: Path) -> None:
    memories_dir = tmp_path / "memories"
    synced_event = threading.Event()

    def _on_sync(_event_type: str, path: Path) -> None:
        if path.name == "live-note.md":
            synced_event.set()

    with OmnimexDB(tmp_path / "watch.db") as db:
        mgr = MemoryManager(db, memories_dir)
        with CentralMemoryWatcher(mgr, debounce_ms=80, on_sync=_on_sync) as watcher:
            assert watcher.is_running is True
            note_path = memories_dir / "facts" / "live-note.md"
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(
                "---\nid: mem_watch_1\ntitle: Watchdog live sync\ncategory: fact\n---\n\nLive watcher updated SQLite.\n",
                encoding="utf-8",
            )
            assert synced_event.wait(timeout=5.0)

        fetched = mgr.get_memory("mem_watch_1")
        assert fetched is not None
        assert "Live watcher updated SQLite." in fetched.content

        # Watcher must support clean restart after stop()
        watcher.start()
        assert watcher.is_running is True
        watcher.stop()
        assert watcher.is_running is False


# ---------------------------------------------------------------------------
# Native Read-Only Bridge
# ---------------------------------------------------------------------------


def test_native_import_bridge_is_strictly_readonly(tmp_path: Path) -> None:
    claude_root = tmp_path / "native_claude"
    claude_mem = claude_root / "projects" / "E--projects-demo" / "memory" / "MEMORY.md"
    claude_mem.parent.mkdir(parents=True)
    claude_bytes = (
        b"---\nname: Never use pnpm\ntype: feedback\n---\n\n"
        b"Use npm in E:/projects/demo; secret ghp_1234567890abcdef1234567890abcdef1234 must scrub.\n"
    )
    claude_mem.write_bytes(claude_bytes)

    codex_root = tmp_path / "native_codex"
    codex_mem = codex_root / "memories" / "codex_tip.md"
    codex_mem.parent.mkdir(parents=True)
    codex_bytes = b"# Codex Note\nAlways check SQLite WAL sidecars before copying.\n"
    codex_mem.write_bytes(codex_bytes)

    codex_sqlite = codex_root / "sqlite" / "memories_1.sqlite"
    codex_sqlite.parent.mkdir(parents=True)
    wal_conn = sqlite3.connect(codex_sqlite)
    try:
        wal_conn.execute("PRAGMA journal_mode = WAL")
        wal_conn.execute(
            "CREATE TABLE stage1_outputs (thread_id TEXT, raw_memory TEXT, rollout_summary TEXT, rollout_slug TEXT, generated_at INTEGER)"
        )
        wal_conn.execute(
            "INSERT INTO stage1_outputs VALUES (?, ?, ?, ?, ?)",
            ("th_1", "Raw memory text", "Summary of stage1", "stage1-slug", 1700000000),
        )
        wal_conn.commit()
        # Keep connection open in WAL mode while importer reads via temp-copy
        claude_mtime_before = claude_mem.stat().st_mtime_ns
        codex_mtime_before = codex_mem.stat().st_mtime_ns

        # Also test non-fallback guard: missing memories/ dir must NOT ingest skills/ or workflows/
        codex_no_mem = tmp_path / "codex_no_mem"
        (codex_no_mem / "skills" / "demo").mkdir(parents=True)
        (codex_no_mem / "skills" / "demo" / "SKILL.md").write_text("# Not a memory\n", encoding="utf-8")
        windsurf_no_mem = tmp_path / "windsurf_no_mem"
        (windsurf_no_mem / "global_workflows").mkdir(parents=True)
        (windsurf_no_mem / "global_workflows" / "flow.md").write_text("# Not a memory\n", encoding="utf-8")

        central_dir = tmp_path / "central_memories"
        with OmnimexDB(tmp_path / "native.db") as db:
            mgr = MemoryManager(db, central_dir)
            importer = NativeMemoryImporter(
                claude_roots=[claude_root],
                codex_roots=[codex_root, codex_no_mem],
                windsurf_roots=[windsurf_no_mem],
            )
            report = mgr.sync_all(import_native=True, native_importer=importer)
            assert report.imported_native == 3
            assert len(report.native_imports) == 2

            imported = mgr.list_memories(origin="native_import")
            assert len(imported) == 3
            assert all(c.native_source_path for c in imported)
            assert all("ghp_123456" not in c.content for c in imported)
            # Central snapshots written only in central_dir
            assert len(list((central_dir / "imported").glob("*.md"))) == 3
    finally:
        wal_conn.close()

    # Native files untouched (same bytes, mtimes, and no extra files written)
    assert claude_mem.read_bytes() == claude_bytes
    assert codex_mem.read_bytes() == codex_bytes
    assert claude_mem.stat().st_mtime_ns == claude_mtime_before
    assert codex_mem.stat().st_mtime_ns == codex_mtime_before
    assert len(list(claude_root.rglob("*"))) == 4  # projects, projdir, memory, MEMORY.md


# ---------------------------------------------------------------------------
# Governance: Dedup, Conflicts, Staleness
# ---------------------------------------------------------------------------


def test_governance_dedup_conflicts_and_staleness(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    src_file = workspace / "src" / "app.py"
    src_file.parent.mkdir(parents=True)
    src_file.write_text("print('v1')\n", encoding="utf-8")

    # Ensure memory updated_at is older than src_file mtime
    old_time = datetime.now(UTC) - timedelta(hours=2)
    future_ts = time.time() + 120
    os.utime(src_file, (future_ts, future_ts))

    c1 = MemoryCard(
        id="m1",
        title="Always use npm in frontend",
        category="preference",
        content="Always use npm scripts in `src/app.py` workspace.",
        workspace=str(workspace),
        updated_at=old_time,
    )
    c2 = MemoryCard(
        id="m2",
        title="Never use npm in frontend",
        category="preference",
        content="Never use npm scripts in `src/app.py`; prefer pnpm instead.",
        workspace=str(workspace),
        updated_at=old_time,
    )
    c1_dup = MemoryCard(
        id="m3",
        title="  always use npm in frontend ",
        category="preference",
        content="Always use npm scripts in `src/app.py` workspace.",
        workspace=str(workspace),
        updated_at=old_time,
    )

    assert compute_content_hash(c1.title, c1.content, c1.category) == compute_content_hash(
        c1_dup.title, c1_dup.content, c1_dup.category
    )
    conflicts = detect_conflicts(c1, [c2])
    assert len(conflicts) == 1
    assert conflicts[0].other_id == "m2"

    staleness = check_staleness(c1, workspace_root=workspace)
    assert staleness.is_stale is True
    assert "src/app.py" in staleness.stale_files

    fresh_card = MemoryCard(
        id="m4",
        title="Fresh note",
        category="fact",
        content="Verified `src/app.py` after edit.",
        workspace=str(workspace),
        updated_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    assert check_staleness(fresh_card, workspace_root=workspace).is_stale is False

    report = audit_memories([c1, c2, c1_dup, fresh_card], workspace_root=workspace)
    assert report.duplicate_groups == [["m1", "m3"]]
    assert len(report.conflicts) == 1
    assert len(report.stale_memories) == 3

    # MemoryManager.save_memory deduplicates identical hashes
    with OmnimexDB(tmp_path / "gov.db") as db:
        mgr = MemoryManager(db, tmp_path / "memories")
        s1 = mgr.save_memory(c1.model_copy(update={"tags": ["a"]}))
        s2 = mgr.save_memory(c1_dup.model_copy(update={"tags": ["b"]}))
        assert s1.id == s2.id
        assert s2.tags == ["a", "b"]


# ---------------------------------------------------------------------------
# Distillers & Backfill Pipeline
# ---------------------------------------------------------------------------


def test_heuristic_ollama_openai_distillers_and_backfill(tmp_path: Path) -> None:
    session = _make_rich_session()

    # 1. Heuristic zero-cost distiller
    heuristic = HeuristicAndAgentDistiller()
    h_cards = heuristic.distill_session(session)
    assert len(h_cards) >= 3
    categories = {c.category for c in h_cards}
    assert "pitfall" in categories
    assert "preference" in categories
    assert any(c.category in ("decision", "fact") for c in h_cards)
    assert all(c.distill_status == "heuristic" for c in h_cards)
    assert all(c.origin == "distilled" for c in h_cards)
    assert all(c.reviewed is False for c in h_cards)
    assert all("sk-secret-should-be-scrubbed-123456" not in c.content for c in h_cards)

    # 2. Ollama distiller (mocked HTTP transport for /api/chat and /v1/chat/completions)
    def _ollama_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        # Transcript must already be redacted before leaving local machine!
        assert "sk-secret-should-be-scrubbed-123456" not in json.dumps(body)
        cards_json = json.dumps(
            [
                {
                    "title": "Quote FTS5 query tokens",
                    "category": "pitfall",
                    "content": "Wrap FTS5 tokens in double quotes to prevent syntax errors on colons.",
                    "tags": ["sqlite", "fts5"],
                    "confidence": 0.94,
                    "source_turn_index": 2,
                }
            ]
        )
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={"message": {"content": cards_json}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": f"```json\n{cards_json}\n```"}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(_ollama_handler)) as mock_client:
        ollama_native = OllamaDistiller(
            base_url="http://127.0.0.1:11434",
            client=mock_client,
        )
        o_cards = ollama_native.distill_session(session)
        assert len(o_cards) == 1
        assert o_cards[0].distill_status == "llm_distilled"
        assert o_cards[0].category == "pitfall"

        ollama_v1 = OllamaDistiller(
            base_url="http://127.0.0.1:11434/v1",
            client=mock_client,
        )
        assert len(ollama_v1.distill_session(session)) == 1

    # 3. OpenAI-compatible distiller (mocked HTTP transport)
    def _openai_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-openai-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "memories": [
                                        {
                                            "title": "Use npm on Windows",
                                            "category": "preference",
                                            "content": "Always use npm rather than pnpm in this workspace.",
                                            "tags": ["windows", "npm"],
                                            "confidence": 0.91,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(_openai_handler)) as oa_client:
        oa_distiller = OpenAICompatibleDistiller(
            base_url="https://api.example.com/v1",
            api_key="test-openai-key",
            client=oa_client,
        )
        oa_cards = oa_distiller.distill_session(session)
        assert len(oa_cards) == 1
        assert oa_cards[0].distill_status == "llm_distilled"
        assert oa_cards[0].category == "preference"

    # 4. Pipeline + backfill upgrade (heuristic -> llm_distilled)
    with OmnimexDB(tmp_path / "pipeline.db") as db:
        db.upsert_session(session)
        mgr = MemoryManager(db, tmp_path / "memories")
        pipeline = DistillationPipeline(db, mgr)

        first_run = pipeline.backfill(limit=10)
        assert first_run.provider == "heuristic"
        assert first_run.processed_sessions == 1
        assert first_run.created_memories >= 3
        stored = db.get_session(session.id, include_turns=False)
        assert stored is not None
        assert stored.distill_status == "heuristic"

        # Second heuristic backfill skips already heuristic-distilled session
        second_run = pipeline.backfill(limit=10)
        assert second_run.processed_sessions == 0

        # Switching to Ollama allows backfill upgrade of heuristic sessions -> llm_distilled
        with httpx.Client(transport=httpx.MockTransport(_ollama_handler)) as upgrade_client:
            pipeline.set_distiller(
                DistillerConfig(provider="ollama", model="qwen2.5:7b-instruct"),
                http_client=upgrade_client,
            )
            progress_events: list[dict[str, object]] = []
            upgraded = pipeline.backfill(limit=10, progress_cb=progress_events.append)
            assert upgraded.provider == "ollama"
            assert upgraded.processed_sessions == 1
            assert upgraded.created_memories == 1
            assert len(progress_events) == 1
            upgraded_session = db.get_session(session.id, include_turns=False)
            assert upgraded_session is not None
            assert upgraded_session.distill_status == "llm_distilled"


# ---------------------------------------------------------------------------
# Handoff Builder
# ---------------------------------------------------------------------------


def test_handoff_builder_formats_and_redacts(tmp_path: Path) -> None:
    session = _make_rich_session()
    with OmnimexDB(tmp_path / "handoff.db") as db:
        db.upsert_session(session)
        mgr = MemoryManager(db, tmp_path / "memories")
        mgr.create_memory(
            title="Enable WAL mode",
            category="decision",
            content="All Omnimex SQLite connections must enable WAL mode.",
            workspace=session.workspace,
            source_session_id=session.id,
        )

        builder = HandoffBuilder(db, mgr)
        for target in ("claude_code", "codex", "antigravity", "cursor", "generic"):
            brief = builder.build(
                session.id,
                target_agent=target,
                custom_instructions="Check Bearer secret_token_1234567890 handling in API routes.",
            )
            assert brief.session_id == session.id
            assert brief.target_agent == target
            assert brief.redacted is True
            assert "secret_token_1234567890" not in brief.markdown
            assert "sk-secret-should-be-scrubbed-123456" not in brief.markdown
            assert "src/omnimex/storage/search.py" in brief.sections.modified_files
            assert len(brief.sections.relevant_memories) >= 1
            assert brief.token_estimate > 0
            assert f"Target Agent:** `{target}`" in brief.markdown

        # Token budget truncation
        short_brief = builder.build(session.id, target_agent="codex", max_tokens=110)
        assert len(short_brief.markdown) <= 460

        with pytest.raises(LookupError):
            builder.build("missing:session")
