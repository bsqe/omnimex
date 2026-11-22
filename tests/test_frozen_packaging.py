"""Tests for packaged (frozen PyInstaller) runtime paths and defaults."""

from __future__ import annotations

import sys
from pathlib import Path

from omnimex.api.app import _resolve_static_dist_dir
from omnimex.cli import _default_skill_path
from omnimex.core.config import load_config
from omnimex.core.runtime_paths import (
    bundle_dir,
    default_user_data_dir,
    exe_dir,
    is_frozen,
)
from omnimex.setup.templates import resolve_omnimex_command
from omnimex.tray import build_app_launch_url


def test_runtime_paths_dev_defaults() -> None:
    """Dev mode reports unfrozen state and resolves paths relative to repo root."""
    assert not is_frozen()
    root = bundle_dir()
    assert (root / "pyproject.toml").is_file()
    assert exe_dir() == root


def test_frozen_config_and_bundle_asset_resolution(monkeypatch, tmp_path: Path) -> None:
    """Frozen mode loads read-only bundled assets while storing state in OMNIMEX_HOME."""
    app_dir = tmp_path / "install" / "Omnimex"
    meipass = app_dir / "_internal"
    user_home = tmp_path / "user-data"
    (meipass / "frontend" / "dist").mkdir(parents=True)
    (meipass / "frontend" / "dist" / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (meipass / "frontend" / "dist" / "assets").mkdir()
    (meipass / "frontend" / "dist" / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
    (meipass / "skills" / "omnimex-memory").mkdir(parents=True)
    (meipass / "skills" / "omnimex-memory" / "SKILL.md").write_text("---\nname: omnimex-memory\n---\n", encoding="utf-8")
    (meipass / "omnimex.toml").write_text("[app]\nport = 9123\n", encoding="utf-8")

    fake_tray_exe = app_dir / "Omnimex.exe"
    fake_cli_name = "omnimex-cli.exe" if sys.platform == "win32" else "omnimex-cli"
    fake_cli_exe = app_dir / fake_cli_name
    fake_tray_exe.write_bytes(b"")
    fake_cli_exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_tray_exe))
    monkeypatch.setenv("OMNIMEX_HOME", str(user_home))
    monkeypatch.delenv("OMNIMEX_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    assert is_frozen()
    assert bundle_dir() == meipass.resolve()
    assert exe_dir() == app_dir.resolve()
    assert default_user_data_dir() == user_home.resolve()

    cfg = load_config()
    assert cfg.app.port == 9123
    assert cfg.root_dir == user_home.resolve()

    static_dir = _resolve_static_dist_dir(cfg)
    assert static_dir == (meipass / "frontend" / "dist").resolve()

    skill_path = _default_skill_path()
    assert skill_path == (meipass / "skills" / "omnimex-memory" / "SKILL.md").resolve()

    url = build_app_launch_url("http://127.0.0.1:9123", cfg)
    assert url.startswith("http://127.0.0.1:9123/?v=")

    cmd, args = resolve_omnimex_command()
    assert Path(cmd).resolve() == fake_cli_exe.resolve()
    assert args == ["mcp"]
