"""Tests for Omnimex Experience-to-Skill builder, archive, ZIP export, installer, MCP, CLI, and API."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from omnimex.api import create_app
from omnimex.cli import app as cli_app
from omnimex.core.config import DistillerConfig, OmnimexConfig
from omnimex.core.models import NormalizedSession, NormalizedTurn, ToolCallRecord
from omnimex.mcp_server import OmnimexMcpTools, create_mcp_server
from omnimex.services import IngestService
from omnimex.skill_forge import (
    SkillBuilder,
    build_skill_zip_bytes,
    install_packaged_skill,
    list_saved_skills,
    parse_skill_frontmatter,
    slugify_skill_name,
)


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
                "auto_distill_on_scan": False,
            },
            "adapters": {
                "watch_enabled": False,
                "scan_on_startup": False,
                "enabled": ["antigravity", "claude_code"],
            },
            "redaction": {"enabled": True},
        }
    )


def _seed_service(tmp_path: Path) -> tuple[IngestService, str, str]:
    svc = IngestService(_make_config(tmp_path))
    sess = NormalizedSession(
        agent="claude_code",
        external_id="win-exe-01",
        title="Fix Windows PyInstaller freeze crash",
        workspace="D:/projects/demo-app",
        source_path="~/.claude/projects/demo/win-exe.jsonl",
        tags=["pyinstaller", "windows"],
        started_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 20, 9, 30, tzinfo=UTC),
        turns=[
            NormalizedTurn(
                turn_index=0,
                role="user",
                content="Package the FastAPI tray app into Windows .exe without stdio crashes.",
                timestamp=datetime(2026, 11, 20, 9, 0, tzinfo=UTC),
            ),
            NormalizedTurn(
                turn_index=1,
                role="assistant",
                content=(
                    "Decided to separate read-only sys._MEIPASS from %LOCALAPPDATA% writable storage.\n"
                    "Avoid passing broken token sk-secret-abcdef12345678901234567890 in config.\n"
                    "Always pass use_colors=False and log_config=None to uvicorn.run in noconsole mode."
                ),
                tool_calls=[
                    ToolCallRecord(
                        id="tc1",
                        name="edit_file",
                        arguments={"Path": "src/demo/tray.py"},
                        output="Updated tray.py",
                        status="ok",
                    ),
                    ToolCallRecord(
                        id="tc2",
                        name="run_command",
                        arguments={"CommandLine": "pytest tests/test_tray.py -q"},
                        output="4 passed",
                        status="ok",
                    ),
                ],
                timestamp=datetime(2026, 11, 20, 9, 25, tzinfo=UTC),
            ),
        ],
    )
    stored = svc.db.upsert_session(sess)
    card = svc.memory_manager.create_memory(
        title="Always separate _MEIPASS and writable app data",
        category="pitfall",
        content="Never write SQLite databases into sys._MEIPASS; use %LOCALAPPDATA% instead.",
        tags=["pyinstaller", "sqlite"],
        workspace="D:/projects/demo-app",
        source_session_id=stored.id,
        source_agent="claude_code",
        reviewed=True,
    )
    return svc, stored.id, card.id


def test_slugify_and_path_traversal_guards() -> None:
    assert slugify_skill_name("Fix Windows PyInstaller Freeze!") == "fix-windows-pyinstaller-freeze"
    assert slugify_skill_name("../../etc/passwd") == "etc-passwd"
    assert slugify_skill_name("解决桌面打包问题", fallback_tags=["pyinstaller", "exe"]) == "pyinstaller-exe"
    assert slugify_skill_name("纯中文标题", seed="abc").startswith("omnimex-skill-")


def test_skill_builder_redacts_secrets_and_supports_llm_polish(tmp_path: Path) -> None:
    svc, sid, mid = _seed_service(tmp_path)
    try:
        draft = svc.generate_skill(
            session_ids=[sid],
            memory_ids=[mid],
            name="Windows PyInstaller Tray Guide",
            custom_instructions="Run isolated temp smoke test before shipping.",
        )
        assert draft.slug == "windows-pyinstaller-tray-guide"
        assert draft.provider == "heuristic"
        assert "sk-secret-" not in draft.skill_md
        assert "[REDACTED_" in draft.skill_md
        parsed_name, parsed_desc, body = parse_skill_frontmatter(draft.skill_md)
        assert parsed_name == "windows-pyinstaller-tray-guide"
        assert "Windows PyInstaller Tray Guide" in parsed_desc
        assert "## Step-by-Step Workflow" in body
        assert "## Pitfalls & Guardrails" in body
        assert "src/demo/tray.py" in body
        assert "pytest tests/test_tray.py -q" in body

        # Mock OpenAI-compatible LLM polish
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "---\nname: ignored-slug\ndescription: ignored\n---\n\n"
                                    "# Polished Windows Tray Skill\n\n"
                                    "## When to Use\n- Packaging windowless FastAPI tray builds.\n"
                                )
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(_handler)
        llm_client = httpx.Client(transport=transport)
        try:
            builder = SkillBuilder(
                svc.db,
                svc.memory_manager,
                distiller_config=DistillerConfig(
                    provider="openai_compatible",
                    model="gpt-4o-mini",
                    api_key="sk-test-token",
                ),
                http_client=llm_client,
            )
            polished = builder.build(memory_ids=[mid], name="Windows Tray Polish", use_llm=True)
            assert polished.provider == "openai_compatible"
            p_name, _, p_body = parse_skill_frontmatter(polished.skill_md)
            # Frontmatter slug must remain normalized from our requested name
            assert p_name == "windows-tray-polish"
            assert "Polished Windows Tray Skill" in p_body
        finally:
            llm_client.close()
    finally:
        svc.close()


def test_skill_save_zip_export_and_multi_agent_install_backups(tmp_path: Path) -> None:
    svc, sid, _ = _seed_service(tmp_path)
    try:
        draft = svc.generate_skill(session_ids=[sid], name="safe-freeze-skill")
        saved = svc.save_skill(draft.slug, draft.skill_md)
        assert Path(saved.file_path).is_file()

        listed = list_saved_skills(svc.config.skills_dir)
        assert [item.slug for item in listed] == ["safe-freeze-skill"]

        clean_slug, zip_bytes = build_skill_zip_bytes(draft.slug, draft.skill_md)
        assert clean_slug == "safe-freeze-skill"
        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            assert zf.namelist() == ["safe-freeze-skill/SKILL.md"]
            assert zf.read("safe-freeze-skill/SKILL.md").decode("utf-8") == saved.skill_md

        fake_home = tmp_path / "home"
        first = install_packaged_skill(
            draft.slug,
            draft.skill_md,
            targets=["antigravity", "claude_code", "codex"],
            home_dir=fake_home,
        )
        assert not first.errors
        assert all(e.status == "created" for e in first.installed)
        ag_skill = fake_home / ".gemini" / "antigravity" / "skills" / "safe-freeze-skill" / "SKILL.md"
        assert ag_skill.is_file()

        # Re-installing identical content reports unchanged cleanly
        idem = install_packaged_skill(
            draft.slug,
            draft.skill_md,
            targets=["antigravity"],
            home_dir=fake_home,
        )
        assert idem.installed[0].status == "unchanged"

        # Updating content creates .bak backup
        updated_md = draft.skill_md + "\n- Added final verification guardrail.\n"
        second = install_packaged_skill(
            draft.slug,
            updated_md,
            targets=["antigravity"],
            home_dir=fake_home,
        )
        assert second.installed[0].status == "updated"
        assert second.installed[0].backup_path is not None
        assert Path(second.installed[0].backup_path).is_file()

        assert svc.delete_skill("safe-freeze-skill") is True
        assert list_saved_skills(svc.config.skills_dir) == []
    finally:
        svc.close()


def test_mcp_and_cli_skill_packaging(tmp_path: Path) -> None:
    svc, sid, mid = _seed_service(tmp_path)
    try:
        tools = OmnimexMcpTools(
            db=svc.db,
            memory_manager=svc.memory_manager,
            searcher=svc.searcher,
            handoff_builder=svc.handoff_builder,
            skill_builder=svc.skill_builder,
            skills_dir=svc.config.skills_dir,
        )
        res = tools.skill_package(session_ids=[sid], memory_ids=[mid], name="mcp-packaged-skill", save=True)
        assert res["created"] is True
        assert res["skill"]["slug"] == "mcp-packaged-skill"
        assert Path(res["saved"]["file_path"]).is_file()

        mcp, _ = create_mcp_server(tools=tools)
        comp_names = {
            comp.name for key, comp in mcp._local_provider._components.items() if str(key).startswith("tool:")
        }
        assert "skill_package" in comp_names
    finally:
        svc.close()

    # CLI build & list against the persisted DB/config
    cfg_file = tmp_path / "omnimex.toml"
    cfg_file.write_text(
        '[storage]\ndb_path = "data/omnimex.db"\nmemories_dir = "data/memories"\n\n[embedding]\nprovider = "offline_hash"\n',
        encoding="utf-8",
    )
    zip_dest = tmp_path / "out" / "cli-skill.zip"
    fake_home = tmp_path / "cli-home"
    runner = CliRunner()
    build_res = runner.invoke(
        cli_app,
        [
            "skill",
            "build",
            "--config",
            str(cfg_file),
            "--session",
            sid,
            "--name",
            "cli-packaged-skill",
            "--install",
            "cursor",
            "--home",
            str(fake_home),
            "--zip",
            str(zip_dest),
            "--json",
        ],
    )
    assert build_res.exit_code == 0, build_res.output
    payload = json.loads(build_res.output)
    assert payload["skill"]["slug"] == "cli-packaged-skill"
    assert payload["installed"]["installed"][0]["status"] == "created"
    assert zip_dest.is_file()

    list_res = runner.invoke(cli_app, ["skill", "list", "--config", str(cfg_file), "--json"])
    assert list_res.exit_code == 0
    listed = json.loads(list_res.output)
    assert any(item["slug"] == "cli-packaged-skill" for item in listed)


def test_api_skills_generate_save_export_install_delete(tmp_path: Path, monkeypatch) -> None:
    svc, sid, mid = _seed_service(tmp_path)
    fake_home = tmp_path / "api-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    app = create_app(
        svc.config,
        ingest_service=svc,
        serve_static=False,
        start_watcher=False,
        scan_on_startup=False,
    )
    try:
        with TestClient(app) as client:
            gen = client.post(
                "/api/skills/generate",
                json={"session_ids": [sid], "memory_ids": [mid], "name": "api-skill-demo"},
            )
            assert gen.status_code == 200
            draft = gen.json()
            assert draft["slug"] == "api-skill-demo"
            assert draft["redacted"] is True

            saved = client.post(
                "/api/skills/save",
                json={"slug": draft["slug"], "skill_md": draft["skill_md"]},
            )
            assert saved.status_code == 201 or saved.status_code == 200
            assert saved.json()["slug"] == "api-skill-demo"

            listed = client.get("/api/skills")
            assert listed.status_code == 200
            assert listed.json()["total"] == 1

            exported = client.post(
                "/api/skills/export",
                json={"slug": draft["slug"], "skill_md": draft["skill_md"]},
            )
            assert exported.status_code == 200
            assert exported.headers["content-type"] == "application/zip"

            installed = client.post(
                "/api/skills/install",
                json={
                    "slug": draft["slug"],
                    "skill_md": draft["skill_md"],
                    "targets": ["claude_code", "antigravity"],
                },
            )
            assert installed.status_code == 200
            assert len(installed.json()["installed"]) == 2
            assert (fake_home / ".claude" / "skills" / "api-skill-demo" / "SKILL.md").is_file()

            deleted = client.delete("/api/skills/api-skill-demo")
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True
    finally:
        svc.close()
