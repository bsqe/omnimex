"""Comprehensive tests for Omnimex mainstream coding-agent adapters and watcher."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

from omnimex.adapters import (
    AdapterRegistry,
    AiderAdapter,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    ClineAdapter,
    CodexAdapter,
    CopilotAdapter,
    CursorAdapter,
    GeminiCliAdapter,
    OpenCodeAdapter,
    RooCodeAdapter,
    SessionWatcher,
    WatchEvent,
    WindsurfAdapter,
    create_default_registry,
)
from omnimex.core.config import DEFAULT_ENABLED_ADAPTERS
from omnimex.storage.db import OmnimexDB

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_default_registry_contains_all_configured_adapters() -> None:
    registry = create_default_registry()
    assert registry.agent_ids() == DEFAULT_ENABLED_ADAPTERS
    assert len(registry.adapters()) == 11
    subset = create_default_registry(enabled=["antigravity", "claude_code", "codex"])
    assert subset.agent_ids() == ["antigravity", "claude_code", "codex"]


def test_antigravity_adapter_hydrates_truncated_fields_and_parses_tools() -> None:
    root = FIXTURES / "antigravity"
    adapter = AntigravityAdapter(search_roots=[root])
    sources = adapter.detect_sources()
    assert len(sources) == 1
    assert sources[0].name == "transcript.jsonl"

    sessions = adapter.scan_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "antigravity:aaaa1111-bbbb-2222-cccc-333344445555"
    assert s.agent == "antigravity"
    assert s.model == "Claude Sonnet 4.5"
    assert s.workspace == "E:/demo/omnimex"
    assert "Inspect the storage WAL helper" in s.title

    # CONVERSATION_HISTORY (empty) is dropped; 6 active steps remain.
    assert len(s.turns) == 6
    assert s.turns[0].role == "user"
    assert "<USER_REQUEST>" not in s.turns[0].content

    # Step 2 had truncated_fields in transcript.jsonl and was hydrated from transcript_full.jsonl
    assert s.turns[1].role == "assistant"
    assert "Running the storage test suite" in s.turns[1].content
    assert s.turns[1].thinking == "First check existing tests before editing storage helpers."
    assert s.turns[1].tool_calls[0].name == "run_command"
    assert s.turns[1].tool_calls[0].arguments == {"CommandLine": "pytest -q"}

    # Step 3 GENERIC -> tool, Step 4 RUN_COMMAND -> tool with grep_search arguments
    assert s.turns[2].role == "tool"
    assert "12 passed" in s.turns[2].content
    assert s.turns[3].role == "tool"
    assert s.turns[3].tool_calls[0].name == "grep_search"
    assert s.turns[3].tool_calls[0].arguments == {"query": "journal_mode"}

    # Step 5 PLANNER_RESPONSE -> assistant with summed token_count
    assert s.turns[4].role == "assistant"
    assert s.turns[4].token_count == 225
    assert s.turns[5].role == "system"


def test_antigravity_handles_windows_permission_error_gracefully(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    readable_logs = brain / "readable-id" / ".system_generated" / "logs"
    locked_dir = brain / "locked-id"
    readable_logs.mkdir(parents=True)
    locked_dir.mkdir(parents=True)

    sample_src = (
        FIXTURES
        / "antigravity/brain/aaaa1111-bbbb-2222-cccc-333344445555/.system_generated/logs/transcript.jsonl"
    )
    shutil.copy2(sample_src, readable_logs / "transcript.jsonl")

    real_scandir = os.scandir

    def fake_scandir(path: Path | str, *args: object, **kwargs: object) -> object:
        if Path(path) == locked_dir:
            raise PermissionError(5, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    adapter = AntigravityAdapter(search_roots=[tmp_path])
    with patch("omnimex.adapters.base.os.scandir", side_effect=fake_scandir):
        health = adapter.health_status()
        sessions = adapter.scan_sessions()

    assert len(sessions) == 1
    assert health.discovered_files == 1
    assert health.status == "degraded"
    assert any("permission_denied:" in err for err in health.errors)

    # Fully locked brain root reports permission_denied cleanly without raising
    locked_root = tmp_path / "locked_only"
    locked_brain = locked_root / "brain"
    locked_brain.mkdir(parents=True)
    adapter_locked = AntigravityAdapter(search_roots=[locked_root])

    def always_deny(path: Path | str, *args: object, **kwargs: object) -> object:
        if Path(path) == locked_brain:
            raise PermissionError(5, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)  # type: ignore[arg-type]

    with patch("omnimex.adapters.base.os.scandir", side_effect=always_deny):
        status_locked = adapter_locked.health_status()

    assert status_locked.status == "permission_denied"
    assert status_locked.discovered_files == 0


def test_claude_code_adapter_and_native_memories() -> None:
    root = FIXTURES / "claude_code"
    adapter = ClaudeCodeAdapter(search_roots=[root])
    sources = adapter.detect_sources()
    assert len(sources) == 1

    sessions = adapter.scan_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "claude_code:sess-claude-001"
    assert s.workspace == "E:/demo/omnimex"
    assert s.model == "claude-sonnet-4-5"
    assert "main" in s.tags
    assert "<system-reminder>" not in s.turns[0].content
    assert s.turns[0].role == "user"
    assert s.turns[1].role == "assistant"
    assert s.turns[1].thinking == "Check existing schema in storage/db.py first."
    assert s.turns[1].tool_calls[0].name == "Read"
    assert s.turns[1].token_count == 265
    assert s.turns[2].role == "tool"
    assert "CREATE VIRTUAL TABLE turns_fts" in s.turns[2].content
    # Human queued prompt included; peer queued prompt & summary excluded
    assert any("unicode61 tokenizer" in t.content for t in s.turns if t.role == "user")
    assert not any("Internal peer relay" in t.content for t in s.turns)

    memories = adapter.discover_native_memories()
    assert any(
        m.title == "pref-pip-windows"
        and m.category == "preference"
        and m.origin == "native_import"
        and m.reviewed is False
        for m in memories
    )


def test_codex_adapter_rollout_and_native_memories() -> None:
    root = FIXTURES / "codex"
    adapter = CodexAdapter(search_roots=[root])
    sources = adapter.detect_sources()
    assert len(sources) == 1

    sessions = adapter.scan_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "codex:codex-sess-001"
    assert s.title == "Codex RRF Hybrid Search"
    assert s.workspace == "E:/demo/omnimex"
    assert s.model == "gpt-5-codex"
    assert "main" in s.tags

    # Host envelope suppressed; cross-schema cx-u1 deduplicated
    user_turns = [t for t in s.turns if t.role == "user"]
    assert len(user_turns) == 1
    assert user_turns[0].content == "Implement RRF hybrid search ranking."
    assert not any("AGENTS.md instructions" in t.content for t in s.turns)

    # Function call linked with output + reasoning attached + token_count populated
    fc_turn = next(t for t in s.turns if t.tool_calls)
    assert fc_turn.tool_calls[0].name == "exec_command"
    assert fc_turn.tool_calls[0].output == "6 passed in 0.21s"
    assert fc_turn.thinking == "Combine BM25 rank and cosine similarity with k=60."

    final_assistant = [t for t in s.turns if t.role == "assistant"][-1]
    assert final_assistant.token_count == 400

    memories = adapter.discover_native_memories()
    assert any(m.origin == "native_import" and "WAL" in m.content for m in memories)
    assert any(m.id == "native_codex_sqlite_codex-sess-001" for m in memories)


def test_cursor_adapter_and_locked_wal_temp_copy(tmp_path: Path) -> None:
    # Fixture check
    adapter = CursorAdapter(search_roots=[FIXTURES / "cursor"])
    sessions = adapter.scan_sessions()
    ids = {s.external_id for s in sessions}
    assert "comp-headers-001" in ids
    headers_sess = next(s for s in sessions if s.external_id == "comp-headers-001")
    assert headers_sess.workspace == "E:/demo/omnimex"
    assert headers_sess.model == "claude-sonnet-4-5"
    assert len(headers_sess.turns) == 2

    # Live locked WAL SQLite database copy test (source bytes + WAL never mutated)
    ws_dir = tmp_path / "workspaceStorage" / "ws-wal"
    ws_dir.mkdir(parents=True)
    db_path = ws_dir / "state.vscdb"
    writer = sqlite3.connect(db_path)
    writer.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA wal_autocheckpoint = 0;
        CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO cursorDiskKV VALUES (
            'composerData:wal-live-1',
            '{"composerId":"wal-live-1","name":"Live WAL Chat","tabs":[{"bubbles":[{"type":1,"text":"Is WAL preserved?"}]}]}'
        );
        """
    )
    writer.commit()
    try:
        wal_path = Path(f"{db_path}-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        before_db = db_path.read_bytes()
        before_wal = wal_path.read_bytes()

        live_adapter = CursorAdapter(search_roots=[tmp_path])
        live_sessions = live_adapter.scan_sessions()
        assert len(live_sessions) == 1
        assert live_sessions[0].turns[0].content == "Is WAL preserved?"
        assert db_path.read_bytes() == before_db
        assert wal_path.read_bytes() == before_wal
    finally:
        writer.close()


def test_gemini_cli_adapter_json_and_jsonl() -> None:
    adapter = GeminiCliAdapter(search_roots=[FIXTURES / "gemini_cli" / "tmp"])
    sessions = adapter.scan_sessions()
    by_id = {s.external_id: s for s in sessions}
    assert {"gem-legacy-001", "gem-stream-002"} <= set(by_id)

    legacy = by_id["gem-legacy-001"]
    assert legacy.workspace == "E:/demo/omnimex"
    assert [t.role for t in legacy.turns] == ["user", "assistant"]

    streamed = by_id["gem-stream-002"]
    assert [t.role for t in streamed.turns] == ["user", "assistant"]
    assert "Incremental replay" in streamed.turns[1].content


def test_windsurf_adapter_and_native_memories() -> None:
    adapter = WindsurfAdapter(search_roots=[FIXTURES / "windsurf"])
    sessions = adapter.scan_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == "windsurf:ws-cascade-001"
    assert sessions[0].workspace == "E:/demo/omnimex"
    assert len(sessions[0].turns) == 2

    memories = adapter.discover_native_memories()
    assert len(memories) == 1
    assert memories[0].category == "preference"
    assert memories[0].origin == "native_import"


def test_cline_and_roo_code_adapters() -> None:
    cline = ClineAdapter(search_roots=[FIXTURES / "cline"])
    cline_sessions = cline.scan_sessions()
    assert len(cline_sessions) == 1
    cs = cline_sessions[0]
    assert cs.id == "cline:task-cline-001"
    assert cs.title == "Build Omnimex adapter registry"
    assert cs.workspace == "E:/demo/omnimex"
    # Prefers ui_messages.json over api_conversation_history.json
    assert len(cs.turns) == 2
    assert "adapter registry" in cs.turns[0].content

    roo = RooCodeAdapter(search_roots=[FIXTURES / "roo_code"])
    roo_sessions = roo.scan_sessions()
    assert len(roo_sessions) == 1
    rs = roo_sessions[0]
    assert rs.id == "roo_code:task-roo-001"
    assert rs.workspace == "E:/demo/omnimex"
    assert [t.role for t in rs.turns] == ["user", "assistant"]


def test_copilot_adapter_vscode_sqlite_and_cli() -> None:
    adapter = CopilotAdapter(search_roots=[FIXTURES / "copilot"])
    sessions = adapter.scan_sessions()
    ids = {s.external_id for s in sessions}
    assert {"copilot-vsc-jsonl-1", "copilot-sqlite-1", "cli-sess-001"} <= ids
    assert "foreign-ext-1" not in ids

    vsc = next(s for s in sessions if s.external_id == "copilot-vsc-jsonl-1")
    assert vsc.workspace == "E:/demo/omnimex"
    assert vsc.turns[1].tool_calls[0].name == "read_file"

    cli = next(s for s in sessions if s.external_id == "cli-sess-001")
    assert cli.turns[1].tool_calls[0].name == "Read"


def test_opencode_and_aider_adapters() -> None:
    opencode = OpenCodeAdapter(search_roots=[FIXTURES / "opencode"])
    oc_sessions = opencode.scan_sessions()
    by_id = {s.external_id: s for s in oc_sessions}
    assert set(by_id) == {"ses_001", "ses_migrated", "ses_v2only"}
    assert "Migrated 2.x prompt" in by_id["ses_migrated"].turns[0].content
    assert "Obsolete 1.x duplicate" not in by_id["ses_migrated"].turns[0].content
    assert by_id["ses_001"].turns[1].thinking is not None

    aider = AiderAdapter(search_roots=[FIXTURES / "aider"])
    aider_sessions = aider.scan_sessions()
    assert len(aider_sessions) == 1
    a_sess = aider_sessions[0]
    assert a_sess.title == "Aider Chat: aider"
    assert [t.role for t in a_sess.turns] == ["user", "assistant"]
    assert "/add src/omnimex/adapters/base.py" in a_sess.turns[0].content


def test_incremental_byte_cursor_append_holdback_and_db_sync(tmp_path: Path) -> None:
    log_path = (
        tmp_path / "brain" / "sess-incr" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    log_path.parent.mkdir(parents=True)
    line1 = (
        '{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE",'
        '"created_at":"2026-11-16T10:00:00Z","content":"<USER_REQUEST>\\nHello\\n</USER_REQUEST>"}\n'
    )
    line2 = (
        '{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE",'
        '"created_at":"2026-11-16T10:00:01Z","content":"Hi there."}\n'
    )
    log_path.write_text(line1 + line2, encoding="utf-8")

    adapter = AntigravityAdapter(search_roots=[tmp_path])
    with OmnimexDB(tmp_path / "test.db") as db:
        res1 = adapter.parse_file_incremental(log_path, db.get_cursor(log_path))
        assert res1.changed is True
        assert res1.is_append_only is False
        assert len(res1.sessions) == 1
        assert [t.turn_index for t in res1.sessions[0].turns] == [0, 1]
        db.upsert_session(res1.sessions[0], replace_turns=not res1.is_append_only)
        db.upsert_cursor(res1.cursor)

        # Unchanged file -> zero turns re-parsed
        res2 = adapter.parse_file_incremental(log_path, db.get_cursor(log_path))
        assert res2.changed is False
        assert res2.sessions == []

        # Partial write without trailing newline -> held back safely
        partial = '{"step_index":2,"source":"USER_EXPLICIT","type":"USER_INPUT","content":"Next'
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(partial)
        res3 = adapter.parse_file_incremental(log_path, db.get_cursor(log_path))
        assert res3.sessions == []
        assert res3.cursor.byte_offset == res1.cursor.byte_offset
        db.upsert_cursor(res3.cursor)

        # Complete the partial line + append another -> incremental append only
        rest = ' question"}\n{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","content":"Next answer"}\n'
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(rest)
        res4 = adapter.parse_file_incremental(log_path, db.get_cursor(log_path))
        assert res4.changed is True
        assert res4.is_append_only is True
        assert len(res4.sessions) == 1
        assert [t.turn_index for t in res4.sessions[0].turns] == [2, 3]
        db.upsert_session(res4.sessions[0], replace_turns=not res4.is_append_only)
        db.upsert_cursor(res4.cursor)

        stored = db.get_session("antigravity:sess-incr", include_turns=True)
        assert stored is not None
        assert stored.turn_count == 4
        assert [t.content for t in stored.turns] == [
            "Hello",
            "Hi there.",
            "Next question",
            "Next answer",
        ]

        # Truncation/rewrite resets cursor to byte 0
        log_path.write_text(line1, encoding="utf-8")
        res5 = adapter.parse_file_incremental(log_path, db.get_cursor(log_path))
        assert res5.changed is True
        assert res5.is_append_only is False
        assert [t.turn_index for t in res5.sessions[0].turns] == [0]


def test_codex_and_claude_incremental_append_and_redaction(tmp_path: Path) -> None:
    # Codex incremental append preserves session_meta sid across later turns & scrubs secrets in DB
    codex_file = tmp_path / "sessions" / "2026" / "11" / "17" / "rollout-inc.jsonl"
    codex_file.parent.mkdir(parents=True)
    codex_line1 = '{"timestamp":"2026-11-17T08:00:00Z","type":"session_meta","payload":{"id":"cx-inc","cwd":"E:\\\\demo\\\\omnimex"}}\n'
    codex_line2 = '{"timestamp":"2026-11-17T08:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"First prompt"}]}}\n'
    codex_file.write_text(codex_line1 + codex_line2, encoding="utf-8")

    codex = CodexAdapter(search_roots=[tmp_path])
    r1 = codex.parse_file_incremental(codex_file)
    assert r1.changed and len(r1.sessions) == 1
    assert r1.sessions[0].external_id == "cx-inc"

    # Append a turn containing a literal <environment_context> and a synthetic dummy secret token
    with codex_file.open("a", encoding="utf-8") as fh:
        fh.write(
            '{"timestamp":"2026-11-17T08:00:02Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>literal</environment_context>"}]}}\n'
            '{"timestamp":"2026-11-17T08:00:03Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Dummy key sk-test1234567890abcdef scrubbed"}]}}\n'
        )
    r2 = codex.parse_file_incremental(codex_file, r1.cursor)
    assert r2.changed and r2.is_append_only
    assert r2.sessions[0].external_id == "cx-inc"
    assert [t.turn_index for t in r2.sessions[0].turns] == [1, 2]

    with OmnimexDB(tmp_path / "inc.db") as db:
        db.upsert_session(r1.sessions[0], replace_turns=True)
        db.upsert_session(r2.sessions[0], replace_turns=False)
        stored = db.get_session("codex:cx-inc", include_turns=True)
        assert stored is not None
        assert stored.turn_count == 3
        assert "sk-test1234567890abcdef" not in stored.turns[2].content
        assert "[REDACTED_API_KEY]" in stored.turns[2].content

    # Claude Code incremental append
    claude_file = tmp_path / "projects" / "demo" / "sess-c.jsonl"
    claude_file.parent.mkdir(parents=True)
    claude_file.write_text(
        '{"type":"user","sessionId":"cl-inc","cwd":"E:/demo/omnimex","message":{"role":"user","content":"One"},"timestamp":"2026-11-17T08:00:00Z"}\n',
        encoding="utf-8",
    )
    claude = ClaudeCodeAdapter(search_roots=[tmp_path])
    c1 = claude.parse_file_incremental(claude_file)
    assert len(c1.sessions) == 1
    with claude_file.open("a", encoding="utf-8") as fh:
        fh.write(
            '{"type":"assistant","sessionId":"cl-inc","message":{"role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"text","text":"Two"}]},"timestamp":"2026-11-17T08:00:01Z"}\n'
        )
    c2 = claude.parse_file_incremental(claude_file, c1.cursor)
    assert c2.is_append_only and c2.sessions[0].external_id == "cl-inc"
    assert [t.turn_index for t in c2.sessions[0].turns] == [1]


def test_session_watcher_debounces_and_routes_events(tmp_path: Path) -> None:
    watch_root = tmp_path / ".claude" / "projects" / "demo"
    watch_root.mkdir(parents=True)
    target = watch_root / "live.jsonl"

    registry = AdapterRegistry([ClaudeCodeAdapter(search_roots=[tmp_path / ".claude"])])
    events: list[WatchEvent] = []
    fired = threading.Event()

    def _on_event(ev: WatchEvent) -> None:
        events.append(ev)
        fired.set()

    with SessionWatcher(registry, _on_event, watch_paths=[watch_root], debounce_ms=80):
        # Write twice rapidly to verify debouncing coalesces events
        target.write_text('{"type":"user","message":{"role":"user","content":"one"}}\n', encoding="utf-8")
        time.sleep(0.02)
        with target.open("a", encoding="utf-8") as fh:
            fh.write('{"type":"assistant","message":{"role":"assistant","content":"two"}}\n')

        assert fired.wait(timeout=3.0), "Watchdog did not dispatch debounced event in time"

    assert len(events) >= 1
    assert events[-1].agent == "claude_code"
    assert events[-1].path == target


def test_registry_resolves_wal_sidecar_to_primary_sqlite_db(tmp_path: Path) -> None:
    ws_dir = tmp_path / "Cursor" / "User" / "workspaceStorage" / "ws-1"
    ws_dir.mkdir(parents=True)
    db_file = ws_dir / "state.vscdb"
    wal_file = ws_dir / "state.vscdb-wal"
    db_file.write_bytes(b"")
    wal_file.write_bytes(b"")

    registry = create_default_registry(enabled=["cursor"])
    resolved = registry.resolve_adapter_for_path(wal_file)
    assert resolved is not None
    adapter, target = resolved
    assert adapter.agent_id == "cursor"
    assert target == db_file


def test_native_memory_ids_are_unique_across_projects_and_ignores_codex_skills(
    tmp_path: Path,
) -> None:
    # Two Claude Code projects with identical memory/MEMORY.md filenames
    p1 = tmp_path / "claude" / "projects" / "E--alpha" / "memory" / "MEMORY.md"
    p2 = tmp_path / "claude" / "projects" / "E--beta" / "memory" / "MEMORY.md"
    p1.parent.mkdir(parents=True)
    p2.parent.mkdir(parents=True)
    p1.write_text("# Alpha Memory\nAlways run ruff before pytest.\n", encoding="utf-8")
    p2.write_text("# Beta Memory\nPrefer SQLite WAL mode.\n", encoding="utf-8")

    claude = ClaudeCodeAdapter(search_roots=[tmp_path / "claude"])
    claude_mems = claude.discover_native_memories()
    assert len(claude_mems) == 2
    assert len({m.id for m in claude_mems}) == 2
    assert {m.workspace for m in claude_mems} == {"E:/alpha", "E:/beta"}

    # Verify both survive DB persistence without overwriting each other
    with OmnimexDB(tmp_path / "mem.db") as db:
        for card in claude_mems:
            db.upsert_memory(card)
        assert len(db.list_memories(limit=10)) == 2

    # Codex: skills/**/SKILL.md must NOT be ingested when memories/ directory is absent
    codex_root = tmp_path / "codex_no_mem"
    skill_md = codex_root / "skills" / "helper" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# Skill instructions\nDo not ingest as native memory.\n", encoding="utf-8")
    codex_empty = CodexAdapter(search_roots=[codex_root])
    assert codex_empty.discover_native_memories() == []

    # Codex & Windsurf identical stems in separate directories get unique IDs
    c_mem1 = tmp_path / "c1" / "memories" / "MEMORY.md"
    c_mem2 = tmp_path / "c2" / "memories" / "MEMORY.md"
    c_mem1.parent.mkdir(parents=True)
    c_mem2.parent.mkdir(parents=True)
    c_mem1.write_text("Codex root one preference.", encoding="utf-8")
    c_mem2.write_text("Codex root two preference.", encoding="utf-8")
    codex_multi = CodexAdapter(search_roots=[tmp_path / "c1", tmp_path / "c2"])
    codex_mems = codex_multi.discover_native_memories()
    assert len(codex_mems) == 2
    assert len({m.id for m in codex_mems}) == 2

    w_mem1 = tmp_path / "w1" / "memories" / "rules.md"
    w_mem2 = tmp_path / "w2" / "memories" / "rules.md"
    w_mem1.parent.mkdir(parents=True)
    w_mem2.parent.mkdir(parents=True)
    w_mem1.write_text("Windsurf one.", encoding="utf-8")
    w_mem2.write_text("Windsurf two.", encoding="utf-8")
    ws_multi = WindsurfAdapter(search_roots=[tmp_path / "w1", tmp_path / "w2"])
    ws_mems = ws_multi.discover_native_memories()
    assert len(ws_mems) == 2
    assert len({m.id for m in ws_mems}) == 2


def test_session_watcher_can_restart_after_stop(tmp_path: Path) -> None:
    watch_root = tmp_path / ".claude" / "projects" / "demo"
    watch_root.mkdir(parents=True)
    registry = AdapterRegistry([ClaudeCodeAdapter(search_roots=[tmp_path / ".claude"])])
    fired = threading.Event()
    watcher = SessionWatcher(
        registry,
        lambda _ev: fired.set(),
        watch_paths=[watch_root],
        debounce_ms=40,
    )
    watcher.start()
    assert watcher.is_running
    watcher.stop()
    assert not watcher.is_running
    watcher.start()
    assert watcher.is_running
    (watch_root / "restart.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"restarted"}}\n',
        encoding="utf-8",
    )
    assert fired.wait(timeout=3.0)
    watcher.stop()
    assert not watcher.is_running
