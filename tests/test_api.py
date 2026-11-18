"""Tests for Omnimex IngestService, FastAPI REST endpoints, error envelopes, and SSE."""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from omnimex.adapters import (
    AdapterRegistry,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
)
from omnimex.adapters.watcher import WatchEvent
from omnimex.api import create_app
from omnimex.core.config import OmnimexConfig
from omnimex.services import IngestService

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_config(tmp_path: Path, **overrides: object) -> OmnimexConfig:
    data: dict[str, object] = {
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
            "watch_debounce_ms": 60,
            "scan_on_startup": False,
            "enabled": ["antigravity", "claude_code", "codex"],
        },
        "redaction": {"enabled": True},
    }
    data.update(overrides)
    return OmnimexConfig.model_validate(data)


def _setup_fixture_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    ag_root = tmp_path / "sources" / "antigravity"
    claude_root = tmp_path / "sources" / "claude"
    codex_root = tmp_path / "sources" / "codex"
    shutil.copytree(FIXTURES / "antigravity", ag_root)
    shutil.copytree(FIXTURES / "claude_code", claude_root)
    shutil.copytree(FIXTURES / "codex", codex_root)
    return ag_root, claude_root, codex_root


def _build_service(tmp_path: Path) -> tuple[IngestService, Path, Path, Path]:
    cfg = _make_config(tmp_path)
    ag_root, claude_root, codex_root = _setup_fixture_roots(tmp_path)
    registry = AdapterRegistry(
        [
            AntigravityAdapter(search_roots=[ag_root]),
            ClaudeCodeAdapter(search_roots=[claude_root]),
            CodexAdapter(search_roots=[codex_root]),
        ]
    )
    service = IngestService(cfg, registry=registry)
    return service, ag_root, claude_root, codex_root


def _parse_sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_raw = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_raw = line.split(":", 1)[1].strip()
        if event_name and data_raw:
            frames.append((event_name, json.loads(data_raw)))
    return frames


def test_ingest_service_incremental_scan_append_watcher_and_stats(tmp_path: Path) -> None:
    service, ag_root, claude_root, _ = _build_service(tmp_path)
    try:
        progress_events: list[dict[str, object]] = []
        sub_queue, unsubscribe = service.events.subscribe(topics=["scan", "session", "memory"])
        try:
            first = service.scan(progress_cb=progress_events.append)
            assert first.scanned_files >= 3
            assert first.new_sessions >= 3
            assert first.new_turns >= 6
            assert first.updated_sessions == 0
            assert first.imported_native_memories >= 1
            assert len(progress_events) == first.scanned_files

            # Incremental rescan with no changes skips everything
            second = service.scan()
            assert second.new_sessions == 0
            assert second.new_turns == 0
            assert second.updated_sessions == 0

            # Append 2 JSONL turns to Claude fixture copy -> incremental ingest only new turns
            claude_jsonl = next((claude_root / "projects").rglob("*.jsonl"))
            with claude_jsonl.open("a", encoding="utf-8") as fh:
                fh.write(
                    '{"type":"user","sessionId":"sess-claude-001","cwd":"E:/demo/omnimex",'
                    '"message":{"role":"user","content":"Always verify ruff clean before pytest."},'
                    '"timestamp":"2026-11-17T09:10:00Z"}\n'
                    '{"type":"assistant","sessionId":"sess-claude-001",'
                    '"message":{"role":"assistant","model":"claude-sonnet-4-5",'
                    '"content":[{"type":"text","text":"Confirmed: always run ruff check first."}]},"timestamp":"2026-11-17T09:10:05Z"}\n'
                )

            third = service.scan()
            assert third.new_sessions == 0
            assert third.updated_sessions == 1
            assert third.new_turns == 2

            # Direct watch callback incremental update on Antigravity transcript
            ag_log = next(ag_root.rglob("transcript.jsonl"))
            with ag_log.open("a", encoding="utf-8") as fh:
                fh.write(
                    '{"step_index":99,"source":"USER_EXPLICIT","type":"USER_INPUT",'
                    '"content":"Live watch follow-up question"}\n'
                )
            ag_adapter = service.registry.get("antigravity")
            assert ag_adapter is not None
            watch_res = service.handle_watch_event(
                WatchEvent(agent="antigravity", path=ag_log, event_type="modified", adapter=ag_adapter)
            )
            assert watch_res is not None
            assert watch_res.updated_sessions == 1
            assert watch_res.new_turns == 1

            # Verify queued SSE frames include started, session.ingested, memory.synced, completed
            emitted_names: list[str] = []
            while not sub_queue.empty():
                item = sub_queue.get_nowait()
                if item is not None:
                    emitted_names.append(item.event)
            assert "scan.started" in emitted_names
            assert "session.ingested" in emitted_names
            assert "memory.synced" in emitted_names
            assert "scan.completed" in emitted_names
        finally:
            unsubscribe()

        stats = service.get_stats()
        assert stats["total_sessions"] == first.new_sessions
        assert stats["total_turns"] >= first.new_turns + 3
        assert stats["index_embeddings_count"] == stats["total_turns"]
        assert stats["total_native_memories"] >= 1
        assert stats["distiller"]["zero_cost_mode"] is True
        assert any(row["agent"] == "claude_code" and row["name"] == "Claude Code" for row in stats["by_agent"])

        statuses = service.get_adapter_statuses()
        by_id = {st.id: st for st in statuses}
        assert by_id["claude_code"].session_count >= 1
        assert by_id["claude_code"].last_scan_at is not None
    finally:
        service.close()


def test_ingest_service_absorbs_permission_error_and_masks_api_key(tmp_path: Path) -> None:
    service, _, _, _ = _build_service(tmp_path)
    try:
        ag = service.registry.get("antigravity")
        assert ag is not None
        with patch.object(ag, "detect_sources", side_effect=PermissionError("WinError 5 denied")):
            res = service.scan(adapters=["antigravity"], import_native=False)
        assert any(err.agent == "antigravity" and "WinError 5" in err.error for err in res.errors)

        updated = service.update_distiller_settings(
            provider="openai_compatible",
            model="gpt-4o-mini",
            model_provided=True,
            base_url="https://api.example.com/v1",
            base_url_provided=True,
            api_key="sk-write-only-secret-12345",
            api_key_provided=True,
        )
        assert updated["provider"] == "openai_compatible"
        assert updated["api_key_configured"] is True
        assert updated["zero_cost_mode"] is False
        assert "api_key" not in updated
        assert (tmp_path / "omnimex.local.toml").is_file()
    finally:
        service.close()


def test_api_rest_endpoints_crud_search_handoff_errors_and_settings(tmp_path: Path) -> None:
    service, _, _, _ = _build_service(tmp_path)
    app = create_app(
        service.config,
        ingest_service=service,
        serve_static=False,
        start_watcher=False,
        scan_on_startup=False,
    )
    try:
        with TestClient(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "ok",
                "version": app.version,
                "db_ready": True,
                "distiller_provider": "heuristic",
            }

            # Initial scan (empty request body works)
            scan_res = client.post("/api/scan")
            assert scan_res.status_code == 200
            scan_data = scan_res.json()
            assert scan_data["status"] == "completed"
            assert scan_data["new_sessions"] >= 3

            # Adapters telemetry
            adapters_res = client.get("/api/adapters")
            assert adapters_res.status_code == 200
            adapters_data = adapters_res.json()
            assert adapters_data["watcher_running"] is False
            assert adapters_data["total_discovered_sources"] == sum(
                a["discovered_files"] for a in adapters_data["adapters"]
            )

            # Hybrid / FTS / Vector search + workspace Windows backslash equivalence
            for mode in ("hybrid", "fts", "vector"):
                r = client.get(
                    "/api/search",
                    params={
                        "q": "hybrid search sqlite",
                        "mode": mode,
                        "workspace": r"E:\demo\omnimex",
                    },
                )
                assert r.status_code == 200
                body = r.json()
                assert body["mode"] == mode
                assert body["total"] >= 1
                assert "agents" in body["facets"]

            # Sessions list & detail & 404 envelope
            sess_list = client.get("/api/sessions", params={"agent": "claude_code,codex"})
            assert sess_list.status_code == 200
            sessions = sess_list.json()["sessions"]
            assert len(sessions) >= 2
            target_sid = sessions[0]["id"]

            detail = client.get(f"/api/sessions/{target_sid}")
            assert detail.status_code == 200
            assert detail.json()["session"]["id"] == target_sid
            assert len(detail.json()["session"]["turns"]) >= 1

            missing_sess = client.get("/api/sessions/does_not_exist:999")
            assert missing_sess.status_code == 404
            assert missing_sess.json()["error"]["code"] == "NOT_FOUND"

            # Memories CRUD + secret redaction + sync + validation error
            bad_mem = client.post(
                "/api/memories",
                json={"title": "   ", "category": "fact", "content": "Valid"},
            )
            assert bad_mem.status_code == 422
            assert bad_mem.json()["error"]["code"] == "VALIDATION_ERROR"

            created = client.post(
                "/api/memories",
                json={
                    "title": "API Key Redaction Policy",
                    "category": "pitfall",
                    "content": "Never log token ghp_1234567890abcdef1234567890abcdef1234 in responses.",
                    "tags": ["security"],
                    "workspace": r"E:\demo\omnimex",
                    "source_session_id": target_sid,
                    "source_agent": "codex",
                    "reviewed": False,
                },
            )
            assert created.status_code == 201
            mem = created.json()
            assert "ghp_1234567890abcdef" not in mem["content"]
            assert "[REDACTED_" in mem["content"]
            mem_id = mem["id"]

            listed = client.get("/api/memories", params={"q": "Redaction"})
            assert listed.status_code == 200
            list_body = listed.json()
            assert list_body["total"] >= 1
            assert "counts_by_category" in list_body
            assert "counts_by_status" in list_body

            patched = client.patch(
                f"/api/memories/{mem_id}",
                json={"reviewed": True, "category": "decision", "title": "Redaction Decision"},
            )
            assert patched.status_code == 200
            assert patched.json()["reviewed"] is True
            assert patched.json()["category"] == "decision"

            synced = client.post("/api/memories/sync")
            assert synced.status_code == 200
            assert synced.json()["synced_files"] >= 1

            deleted = client.delete(f"/api/memories/{mem_id}")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True, "id": mem_id}

            missing_patch = client.patch(f"/api/memories/{mem_id}", json={"reviewed": True})
            assert missing_patch.status_code == 404
            assert missing_patch.json()["error"]["code"] == "NOT_FOUND"

            # Handoff 200 + 404
            handoff = client.post(
                "/api/handoff",
                json={
                    "session_id": target_sid,
                    "target_agent": "claude_code",
                    "custom_instructions": "Verify API contract alignment.",
                },
            )
            assert handoff.status_code == 200
            assert handoff.json()["target_agent"] == "claude_code"
            assert "## " in handoff.json()["markdown"]

            missing_handoff = client.post(
                "/api/handoff",
                json={"session_id": "missing:123", "target_agent": "codex"},
            )
            assert missing_handoff.status_code == 404
            assert missing_handoff.json()["error"]["code"] == "NOT_FOUND"

            # Distiller settings write-only api_key & backfill
            patch_dist = client.patch(
                "/api/settings/distiller",
                json={
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-api-writeonly-abcdef987654",
                    "auto_distill_on_scan": False,
                },
            )
            assert patch_dist.status_code == 200
            pd_json = patch_dist.json()
            assert pd_json["api_key_configured"] is True
            assert "api_key" not in pd_json
            assert "sk-api-writeonly" not in patch_dist.text

            get_dist = client.get("/api/settings/distiller")
            assert get_dist.status_code == 200
            assert "api_key" not in get_dist.json()
            assert get_dist.json()["api_key_configured"] is True

            # Switch back to heuristic and run backfill
            client.patch(
                "/api/settings/distiller",
                json={"provider": "heuristic", "api_key": None, "auto_distill_on_scan": True},
            )
            backfill = client.post("/api/distill/backfill", json={"limit": 10, "force": True})
            assert backfill.status_code == 200
            assert backfill.json()["status"] == "completed"
            assert backfill.json()["provider"] == "heuristic"

            # Stats response shape
            stats = client.get("/api/stats")
            assert stats.status_code == 200
            stats_json = stats.json()
            assert stats_json["total_sessions"] >= 3
            assert isinstance(stats_json["recent_activity"], list)
            assert stats_json["distiller"]["provider"] == "heuristic"
    finally:
        service.close()


def test_api_sse_streams_cors_and_static_spa_isolation(tmp_path: Path) -> None:
    service, _, _, _ = _build_service(tmp_path)
    static_dir = tmp_path / "dist"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html><body>Omnimex UI</body></html>", encoding="utf-8")
    (static_dir / "assets" / "app.js").write_text("console.log('omnimex');", encoding="utf-8")

    app = create_app(
        service.config,
        ingest_service=service,
        serve_static=True,
        static_dir=static_dir,
        start_watcher=False,
        scan_on_startup=False,
    )
    app.state.sse_heartbeat_seconds = 0.15

    try:
        with TestClient(app) as client:
            # 1. Streamed scan SSE
            scan_sse = client.post("/api/scan?stream=true", json={"distill": True})
            assert scan_sse.status_code == 200
            assert "text/event-stream" in scan_sse.headers["content-type"]
            scan_events = [name for name, _ in _parse_sse_frames(scan_sse.text)]
            assert "scan.started" in scan_events
            assert "scan.progress" in scan_events
            assert "scan.completed" in scan_events

            # 2. Streamed backfill SSE
            backfill_sse = client.post(
                "/api/distill/backfill",
                headers={"Accept": "text/event-stream"},
                json={"limit": 5, "force": True},
            )
            assert backfill_sse.status_code == 200
            backfill_events = [name for name, _ in _parse_sse_frames(backfill_sse.text)]
            assert "distill.completed" in backfill_events

            # 3. Unified /api/events SSE stream (connected + pushed event + heartbeat)
            def _push_later() -> None:
                time.sleep(0.02)
                service.events.publish("memory.synced", {"synced_files": 4, "imported_native": 1})

            pusher = threading.Thread(target=_push_later, daemon=True)
            pusher.start()
            seen_events: list[str] = []
            with client.stream("GET", "/api/events?topics=memory") as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        seen_events.append(line.split(":", 1)[1].strip())
                        if len(seen_events) >= 3:
                            break
            pusher.join(timeout=1.0)
            assert seen_events[0] == "connected"
            assert "memory.synced" in seen_events
            assert "heartbeat" in seen_events

            # 4. CORS headers for Vite dev server
            cors_res = client.get(
                "/api/health",
                headers={"Origin": "http://localhost:5173"},
            )
            assert cors_res.headers.get("access-control-allow-origin") == "http://localhost:5173"

            # 5. Static SPA + assets + strict /api/* 404 isolation
            root_html = client.get("/")
            assert root_html.status_code == 200
            assert "Omnimex UI" in root_html.text
            assert "no-store" in root_html.headers.get("cache-control", "")

            spa_route = client.get("/sessions/antigravity:123")
            assert spa_route.status_code == 200
            assert "Omnimex UI" in spa_route.text

            asset_res = client.get("/assets/app.js")
            assert asset_res.status_code == 200
            assert "console.log('omnimex');" in asset_res.text

            api_404 = client.get("/api/nonexistent-endpoint")
            assert api_404.status_code == 404
            assert api_404.json()["error"]["code"] == "NOT_FOUND"
    finally:
        service.close()


def test_api_session_turn_pagination_truncation_and_nonblocking_startup(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from omnimex.core.models import NormalizedSession, NormalizedTurn, ToolCallRecord

    service, _, _, _ = _build_service(tmp_path)
    try:
        # Insert a 10-turn session where turn #8 has >2KB payload for truncation testing
        turns = [
            NormalizedTurn(
                turn_index=i,
                role="user" if i % 2 == 0 else "assistant",
                content=("X" * 2400) if i == 8 else f"Turn message #{i}",
                tool_calls=(
                    [
                        ToolCallRecord(
                            id=f"tc-{i}",
                            name="run_command",
                            arguments={"cmd": "echo hi"},
                            output="Y" * 1800,
                            status="ok",
                        )
                    ]
                    if i == 8
                    else []
                ),
                timestamp=datetime(2026, 11, 17, 10, i, 0, tzinfo=UTC),
            )
            for i in range(10)
        ]
        big_sess = NormalizedSession(
            agent="codex",
            external_id="paginated-001",
            title="Large paginated session",
            workspace="E:/demo/omnimex",
            source_path="~/.codex/sessions/paginated.jsonl",
            turns=turns,
        )
        service.db.upsert_session(big_sess)

        app = create_app(
            service.config,
            ingest_service=service,
            serve_static=False,
            start_watcher=False,
            scan_on_startup=True,
        )
        with TestClient(app) as client:
            # Startup scan runs in background thread without blocking /api/health
            health = client.get("/api/health")
            assert health.status_code == 200

            # Default tail window returns last 3 turns when turn_limit=3
            tail_res = client.get(
                f"/api/sessions/{big_sess.id}",
                params={"turn_limit": 3, "max_turn_chars": 800},
            )
            assert tail_res.status_code == 200
            tail_json = tail_res.json()
            assert tail_json["total_turns"] == 10
            assert tail_json["turn_offset"] == 7
            assert tail_json["has_earlier_turns"] is True
            assert tail_json["has_later_turns"] is False
            assert [t["turn_index"] for t in tail_json["session"]["turns"]] == [7, 8, 9]
            assert 8 in tail_json["truncated_turn_indices"]
            assert "[truncated" in tail_json["session"]["turns"][1]["content"]

            # Load earlier page (offset=4, limit=3)
            prev_res = client.get(
                f"/api/sessions/{big_sess.id}",
                params={"turn_offset": 4, "turn_limit": 3, "max_turn_chars": 800},
            )
            assert prev_res.status_code == 200
            prev_json = prev_res.json()
            assert prev_json["has_earlier_turns"] is True
            assert prev_json["has_later_turns"] is True
            assert [t["turn_index"] for t in prev_json["session"]["turns"]] == [4, 5, 6]

            # Fetch full untruncated turn #8 on demand
            full_turn = client.get(f"/api/sessions/{big_sess.id}/turns/8")
            assert full_turn.status_code == 200
            assert full_turn.json()["truncated"] is False
            assert len(full_turn.json()["turn"]["content"]) == 2400
            assert len(full_turn.json()["turn"]["tool_calls"][0]["output"]) == 1800

            # Handoff on large session succeeds cleanly
            hb = client.post(
                "/api/handoff",
                json={"session_id": big_sess.id, "target_agent": "antigravity"},
            )
            assert hb.status_code == 200
            assert hb.json()["session_id"] == big_sess.id

            startup_t = getattr(app.state, "startup_scan_thread", None)
            if startup_t is not None:
                startup_t.join(timeout=5.0)
    finally:
        service.close()


def test_api_session_qa_only_filters_intermediate_process_turns(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from omnimex.core.models import NormalizedSession, NormalizedTurn, ToolCallRecord

    service, _, _, _ = _build_service(tmp_path)
    try:
        turns = [
            NormalizedTurn(
                turn_index=0,
                role="user",
                content="Question 1",
                timestamp=datetime(2026, 11, 17, 11, 0, 0, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=1,
                role="assistant",
                content="",
                thinking="Hidden reasoning step",
                tool_calls=[
                    ToolCallRecord(id="tc1", name="Bash", arguments={}, output="", status="ok")
                ],
                timestamp=datetime(2026, 11, 17, 11, 0, 5, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=2,
                role="tool",
                content="stdout dump",
                timestamp=datetime(2026, 11, 17, 11, 0, 10, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=3,
                role="system",
                content="Compaction system note",
                timestamp=datetime(2026, 11, 17, 11, 0, 12, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=4,
                role="assistant",
                content="[Tool: exec_command]",
                thinking="Codex tool step",
                timestamp=datetime(2026, 11, 17, 11, 0, 15, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=5,
                role="assistant",
                content="Final clean answer",
                thinking="Answer reasoning attached",
                timestamp=datetime(2026, 11, 17, 11, 0, 20, tzinfo=UTC),
            ),
        ]
        sess = NormalizedSession(
            agent="claude_code",
            external_id="qa-filter-001",
            title="QA filter test session",
            workspace="E:/demo/omnimex",
            source_path="~/.claude/projects/demo/qa.jsonl",
            turns=turns,
        )
        service.db.upsert_session(sess)

        app = create_app(
            service.config,
            ingest_service=service,
            serve_static=False,
            start_watcher=False,
            scan_on_startup=False,
        )
        with TestClient(app) as client:
            qa_res = client.get(
                f"/api/sessions/{sess.id}",
                params={"qa_only": "true", "turn_limit": 40},
            )
            assert qa_res.status_code == 200
            qa_json = qa_res.json()
            assert qa_json["total_turns"] == 2
            assert qa_json["session"]["turn_count"] == 6
            assert [t["turn_index"] for t in qa_json["session"]["turns"]] == [0, 5]
            assert qa_json["session"]["turns"][1]["thinking"] == "Answer reasoning attached"

            full_res = client.get(
                f"/api/sessions/{sess.id}",
                params={"qa_only": "false", "turn_limit": 40},
            )
            assert full_res.status_code == 200
            full_json = full_res.json()
            assert full_json["total_turns"] == 6
            assert [t["turn_index"] for t in full_json["session"]["turns"]] == [0, 1, 2, 3, 4, 5]
    finally:
        service.close()


def test_api_list_sessions_filters_subagent_sessions_when_requested(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from omnimex.core.models import NormalizedSession, NormalizedTurn

    service, _, _, _ = _build_service(tmp_path)
    try:
        human_sess = NormalizedSession(
            agent="antigravity",
            external_id="human-001",
            title="Human question session",
            workspace="E:/demo/omnimex",
            source_path="~/.gemini/antigravity/conversations/human.pb",
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="user",
                    content="Build Omnimex",
                    timestamp=datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC),
                ),
                NormalizedTurn(
                    turn_index=1,
                    role="assistant",
                    content="Done.",
                    timestamp=datetime(2026, 11, 17, 12, 1, 0, tzinfo=UTC),
                ),
            ],
        )
        subagent_sess = NormalizedSession(
            agent="antigravity",
            external_id="subagent-001",
            title="The following is a <SYSTEM_MESSAGE> not actually sent by the user.",
            workspace="",
            source_path="~/.gemini/antigravity/conversations/subagent.pb",
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="system",
                    content="The following is a <SYSTEM_MESSAGE> not actually sent by the user.",
                    timestamp=datetime(2026, 11, 17, 12, 2, 0, tzinfo=UTC),
                ),
                NormalizedTurn(
                    turn_index=1,
                    role="assistant",
                    content="",
                    thinking="Running browser check.",
                    timestamp=datetime(2026, 11, 17, 12, 3, 0, tzinfo=UTC),
                ),
            ],
        )
        service.db.upsert_session(human_sess)
        service.db.upsert_session(subagent_sess)

        app = create_app(
            service.config,
            ingest_service=service,
            serve_static=False,
            start_watcher=False,
            scan_on_startup=False,
        )
        with TestClient(app) as client:
            all_res = client.get("/api/sessions")
            assert all_res.status_code == 200
            assert all_res.json()["total"] == 2

            filtered_res = client.get("/api/sessions", params={"include_subagents": "false"})
            assert filtered_res.status_code == 200
            payload = filtered_res.json()
            assert payload["total"] == 1
            assert [s["id"] for s in payload["sessions"]] == [human_sess.id]
    finally:
        service.close()
