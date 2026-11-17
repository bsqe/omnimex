"""Tests for Omnimex FastMCP server tools, safe client setup installer, SKILL.md, and Typer CLI."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omnimex.cli import app as cli_app
from omnimex.core.models import (
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
)
from omnimex.mcp_server import OmnimexMcpTools, create_mcp_server
from omnimex.setup import (
    SUPPORTED_MCP_CLIENTS,
    configure_mcp_clients,
    export_agent_rules,
    install_skill,
)
from omnimex.storage.db import OmnimexDB
from omnimex.storage.embeddings import OfflineHashEmbedder


def _seed_db(db: OmnimexDB, embedder: OfflineHashEmbedder) -> NormalizedSession:
    t0 = datetime(2026, 11, 17, 7, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 17, 7, 5, 0, tzinfo=UTC)
    session = NormalizedSession(
        agent="codex",
        external_id="mcp-sess-1",
        title="Wire SQLite WAL and FastMCP progressive tools",
        workspace=r"E:\projects\omnimex",
        source_path="~/.codex/sessions/2026/11/17/rollout-mcp.jsonl",
        started_at=t0,
        updated_at=t1,
        turns=[
            NormalizedTurn(
                turn_index=0,
                role="user",
                content="Always prefer progressive disclosure in MCP tools.",
                timestamp=t0,
            ),
            NormalizedTurn(
                turn_index=1,
                role="assistant",
                content="Implemented history_search and memory_recall with SQLite WAL FTS5.",
                thinking="Scrub secret sk-secret1234567890abcdef before storing.",
                tool_calls=[
                    ToolCallRecord(
                        id="tc1",
                        name="edit_file",
                        arguments={"Path": "src/omnimex/mcp_server/tools.py"},
                        output="Updated tools.py",
                        status="ok",
                    )
                ],
                timestamp=t1,
            ),
        ],
    )
    db.upsert_session(session)
    db.index_session_embeddings(session.id, embedder)
    return session


def test_mcp_tools_progressive_disclosure_and_server_registration(tmp_path: Path) -> None:
    embedder = OfflineHashEmbedder()
    with OmnimexDB(tmp_path / "mcp.db") as db:
        session = _seed_db(db, embedder)
        tools = OmnimexMcpTools.from_config(
            db=db,
            embedder=embedder,
        )
        # Rebind memory manager into tmp_path so tests never touch repo data/
        from omnimex.handoff import HandoffBuilder
        from omnimex.memory import MemoryManager

        mgr = MemoryManager(db, tmp_path / "memories", embedder=embedder)
        tools = OmnimexMcpTools(
            db=db,
            memory_manager=mgr,
            searcher=tools.searcher,
            handoff_builder=HandoffBuilder(db, mgr),
        )

        # Save + recall + read + update
        saved_res = tools.memory_save(
            title="Always use npm on Windows",
            category="preference",
            content="Do not run pnpm in frontend; secret ghp_1234567890abcdef1234567890abcdef1234 scrubbed.",
            workspace=r"E:\projects\omnimex",
            tags=["windows", "npm"],
            source_session_id=session.id,
            source_agent="codex",
        )
        assert saved_res["saved"] is True
        mem_id = saved_res["memory"]["id"]
        assert saved_res["memory"]["origin"] == "manual"
        assert saved_res["memory"]["reviewed"] is True
        assert "ghp_123456" not in saved_res["memory"]["content"]
        assert (tmp_path / "memories" / saved_res["memory"]["file_path"]).is_file()

        recalled = tools.memory_recall("npm Windows", workspace="E:/projects/omnimex", limit=5)
        assert recalled["count"] == 1
        assert recalled["memories"][0]["id"] == mem_id
        assert "preview" in recalled["memories"][0]

        read_mem = tools.memory_read(mem_id)
        assert read_mem["found"] is True
        assert read_mem["memory"]["title"] == "Always use npm on Windows"

        updated_mem = tools.memory_update(mem_id, title="Always use npm scripts on Windows")
        assert updated_mem["updated"] is True
        assert updated_mem["memory"]["title"] == "Always use npm scripts on Windows"

        # History search + paginated history read
        hist_hits = tools.history_search("SQLite WAL FTS5", workspace="E:/projects/omnimex", limit=5)
        assert hist_hits["total"] >= 1
        assert hist_hits["hits"][0]["session_id"] == session.id

        hist_page = tools.history_read(session.id, start_turn=1, limit=1)
        assert hist_page["found"] is True
        assert hist_page["returned_turns"] == 1
        assert hist_page["turns"][0]["turn_index"] == 1
        assert "sk-secret1234567890abcdef" not in (hist_page["turns"][0].get("thinking") or "")

        missing_hist = tools.history_read("nonexistent:id")
        assert missing_hist["found"] is False

        # Handoff
        handoff_res = tools.session_handoff(
            session.id,
            target_agent="claude_code",
            custom_instructions="Wire CLI subcommands next.",
        )
        assert handoff_res["found"] is True
        assert handoff_res["handoff"]["target_agent"] == "claude_code"
        assert handoff_res["handoff"]["redacted"] is True

        # FastMCP server registration check (synchronous)
        mcp, _ = create_mcp_server(tools=tools)
        local_tools = mcp._local_provider._components
        registered_names = {
            comp.name for key, comp in local_tools.items() if str(key).startswith("tool:")
        }
        assert {
            "history_search",
            "history_read",
            "memory_recall",
            "memory_read",
            "memory_save",
            "memory_update",
            "session_handoff",
        }.issubset(registered_names)


def test_setup_dry_run_and_apply_with_backups_and_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)

    # Pre-seed Claude Code JSON & Codex TOML to verify non-destructive merge + .bak backup
    claude_cfg = home / ".claude.json"
    claude_cfg.parent.mkdir(parents=True, exist_ok=True)
    claude_cfg.write_text(
        json.dumps({"mcpServers": {"other_tool": {"command": "other", "args": []}}, "theme": "dark"}),
        encoding="utf-8",
    )

    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir(parents=True, exist_ok=True)
    codex_cfg.write_text(
        'model = "gpt-5"\n\n[mcp_servers.existing]\ncommand = "echo"\nargs = ["hi"]\n',
        encoding="utf-8",
    )

    # Dry-run must NOT modify or create files
    dry = configure_mcp_clients(
        SUPPORTED_MCP_CLIENTS,
        apply=False,
        home_dir=home,
        workspace_dir=ws,
        command="omnimex",
    )
    assert len(dry.actions) == 6
    assert dry.errors == []
    assert all(not a.applied for a in dry.actions)
    assert "other_tool" in json.loads(claude_cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert "omnimex" not in json.loads(claude_cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert not (home / ".cursor" / "mcp.json").exists()

    # Apply creates .bak backups and preserves existing entries
    applied = configure_mcp_clients(
        SUPPORTED_MCP_CLIENTS,
        apply=True,
        home_dir=home,
        workspace_dir=ws,
        command="omnimex",
    )
    assert applied.errors == []
    assert all(a.applied for a in applied.actions)
    assert (home / ".claude.json.bak").is_file()
    assert (home / ".codex" / "config.toml.bak").is_file()

    merged_claude = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert merged_claude["theme"] == "dark"
    assert "other_tool" in merged_claude["mcpServers"]
    assert merged_claude["mcpServers"]["omnimex"]["args"] == ["mcp"]

    merged_codex = tomllib.loads(codex_cfg.read_text(encoding="utf-8"))
    assert merged_codex["model"] == "gpt-5"
    assert "existing" in merged_codex["mcp_servers"]
    assert merged_codex["mcp_servers"]["omnimex"]["command"] == "omnimex"

    # Re-applying with Windows backslashes & OMNIMEX_CONFIG env must stay valid TOML
    win_cmd = r"E:\AAAAA project test\omnimex\.venv\Scripts\omnimex.exe"
    win_cfg = tmp_path / "omnimex.toml"
    win_cfg.write_text("", encoding="utf-8")
    for _ in range(2):
        win_res = configure_mcp_clients(
            ["codex"],
            apply=True,
            home_dir=home,
            workspace_dir=ws,
            command=win_cmd,
            omnimex_config_path=win_cfg,
        )
        assert win_res.errors == []
    re_merged_codex = tomllib.loads(codex_cfg.read_text(encoding="utf-8"))
    assert re_merged_codex["mcp_servers"]["omnimex"]["command"] == win_cmd
    assert re_merged_codex["mcp_servers"]["omnimex"]["env"]["OMNIMEX_CONFIG"] == str(win_cfg)

    # AGENTS.md / CLAUDE.md rule export
    agents_md = ws / "AGENTS.md"
    agents_md.write_text("# Workspace Rules\n\nExisting rule.\n", encoding="utf-8")
    rules_res = export_agent_rules([agents_md, ws / "CLAUDE.md"], apply=True)
    assert len(rules_res.actions) == 2
    assert (ws / "AGENTS.md.bak").is_file()
    rendered_agents = agents_md.read_text(encoding="utf-8")
    assert "Existing rule." in rendered_agents
    assert "memory_recall" in rendered_agents
    assert "memory_save" in rendered_agents

    # Verify companion SKILL.md exists and has valid YAML frontmatter
    repo_root = Path(__file__).resolve().parents[1]
    skill_path = repo_root / "skills" / "omnimex-memory" / "SKILL.md"
    assert skill_path.is_file()
    skill_text = skill_path.read_text(encoding="utf-8")
    assert skill_text.startswith("---")
    assert "name: omnimex-memory" in skill_text
    assert "memory_recall" in skill_text
    assert "memory_save" in skill_text

    # Install skill dry-run vs apply
    skill_dry = install_skill(skill_path, home_dir=home, apply=False)
    assert len(skill_dry.actions) == 3
    assert not (home / ".codex" / "skills" / "omnimex-memory" / "SKILL.md").exists()
    skill_apply = install_skill(skill_path, home_dir=home, apply=True)
    assert all(a.applied for a in skill_apply.actions)
    assert (home / ".codex" / "skills" / "omnimex-memory" / "SKILL.md").is_file()


def test_typer_cli_end_to_end_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    claude_dir = fake_home / ".claude" / "projects" / "E--demo-cli"
    claude_dir.mkdir(parents=True)
    session_file = claude_dir / "sess-cli-001.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-11-17T07:00:00Z",
                        "cwd": "E:/demo/cli",
                        "message": {
                            "role": "user",
                            "content": "Always use npm and configure SQLite WAL mode.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-11-17T07:02:00Z",
                        "cwd": "E:/demo/cli",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet-4-5",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Decided to use SQLite WAL with FTS5 hybrid search.",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    cfg_path = tmp_path / "omnimex.toml"
    cfg_path.write_text(
        '[storage]\ndb_path = "data/omnimex.db"\nmemories_dir = "data/memories"\n'
        '[embedding]\nprovider = "offline_hash"\nallow_download = false\n'
        '[distiller]\nprovider = "heuristic"\nauto_distill_on_scan = false\n'
        '[adapters]\nenabled = ["claude_code"]\nwatch_debounce_ms = 50\n',
        encoding="utf-8",
    )

    runner = CliRunner()

    # 1. scan
    scan_res = runner.invoke(cli_app, ["scan", "--config", str(cfg_path), "--json"])
    assert scan_res.exit_code == 0, scan_res.output
    scan_data = json.loads(scan_res.output)
    assert scan_data["new_sessions"] == 1
    assert scan_data["new_turns"] == 2

    # 2. status
    status_res = runner.invoke(cli_app, ["status", "--config", str(cfg_path), "--json"])
    assert status_res.exit_code == 0, status_res.output
    status_data = json.loads(status_res.output)
    assert status_data["stats"]["total_sessions"] == 1
    assert status_data["stats"]["total_turns"] == 2

    # 3. search
    search_res = runner.invoke(
        cli_app, ["search", "SQLite WAL", "--config", str(cfg_path), "--json"]
    )
    assert search_res.exit_code == 0, search_res.output
    search_data = json.loads(search_res.output)
    assert search_data["total"] >= 1
    sess_id = search_data["hits"][0]["session_id"]

    # 4. distill --backfill
    distill_res = runner.invoke(
        cli_app, ["distill", "--backfill", "--config", str(cfg_path), "--json"]
    )
    assert distill_res.exit_code == 0, distill_res.output
    distill_data = json.loads(distill_res.output)
    assert distill_data["processed_sessions"] == 1
    assert distill_data["created_memories"] >= 1

    # 5. handoff
    out_md = tmp_path / "handoff.md"
    handoff_res = runner.invoke(
        cli_app,
        [
            "handoff",
            sess_id,
            "--target",
            "codex",
            "--output",
            str(out_md),
            "--config",
            str(cfg_path),
            "--json",
        ],
    )
    assert handoff_res.exit_code == 0, handoff_res.output
    handoff_data = json.loads(handoff_res.output)
    assert handoff_data["target_agent"] == "codex"
    assert out_md.is_file()

    # 6. watch (bounded duration smoke test)
    watch_res = runner.invoke(
        cli_app,
        ["watch", "--duration", "0.15", "--no-initial-scan", "--config", str(cfg_path)],
    )
    assert watch_res.exit_code == 0, watch_res.output

    # 7. setup --apply --export-rules --install-skill
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    setup_res = runner.invoke(
        cli_app,
        [
            "setup",
            "--client",
            "claude_code,codex",
            "--apply",
            "--export-rules",
            "--install-skill",
            "--home",
            str(fake_home),
            "--workspace",
            str(ws_dir),
            "--command",
            "omnimex",
            "--json",
        ],
    )
    assert setup_res.exit_code == 0, setup_res.output
    setup_data = json.loads(setup_res.output)
    assert setup_data["apply"] is True
    assert setup_data["errors"] == []
    assert (fake_home / ".claude.json").is_file()
    assert (fake_home / ".codex" / "config.toml").is_file()
    assert (ws_dir / "AGENTS.md").is_file()
    assert (fake_home / ".codex" / "skills" / "omnimex-memory" / "SKILL.md").is_file()

    # 8. help checks for mcp & serve
    assert runner.invoke(cli_app, ["mcp", "--help"]).exit_code == 0
    assert runner.invoke(cli_app, ["serve", "--help"]).exit_code == 0
