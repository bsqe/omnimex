"""Tests for Omnimex Windows tray controller, app-window launcher, and icon helpers."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from omnimex.core.config import OmnimexConfig
from omnimex.tray import (
    OmnimexTrayController,
    create_tray_image,
    ensure_tray_ico,
    ensure_windowless_stdio,
    find_app_browser_command,
    open_app_window,
    wait_for_server,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_tray_image_ico_and_windowless_stdio(tmp_path: Path, monkeypatch) -> None:
    img = create_tray_image(64)
    assert img.size == (64, 64)

    ico_path = tmp_path / "data" / "omnimex.ico"
    created = ensure_tray_ico(ico_path)
    assert created.is_file()
    assert created.stat().st_size > 0

    log_path = tmp_path / "data" / "tray.log"
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    ensure_windowless_stdio(log_path)
    assert sys.stdout is not None
    assert sys.stderr is not None
    print("windowless stdout ok")
    sys.stdout.close()


def test_find_and_open_app_browser_window(tmp_path: Path) -> None:
    fake_edge = tmp_path / "msedge.exe"
    fake_edge.write_bytes(b"")

    with patch("omnimex.tray.shutil.which", return_value=str(fake_edge)):
        cmd = find_app_browser_command("http://127.0.0.1:8765")
        assert cmd == [str(fake_edge), "--app=http://127.0.0.1:8765"]

        with patch("omnimex.tray.subprocess.Popen") as mock_popen:
            assert open_app_window("http://127.0.0.1:8765") is True
            mock_popen.assert_called_once()
            assert mock_popen.call_args[0][0] == [str(fake_edge), "--app=http://127.0.0.1:8765"]


def test_tray_controller_reuses_healthy_existing_server(tmp_path: Path) -> None:
    cfg = OmnimexConfig.model_validate(
        {
            "root_dir": tmp_path,
            "storage": {"db_path": "data/omnimex.db", "memories_dir": "data/memories"},
            "embedding": {"provider": "offline_hash", "allow_download": False},
            "adapters": {"watch_enabled": False, "scan_on_startup": False, "enabled": []},
        }
    )
    controller = OmnimexTrayController(cfg, port=18999, open_on_start=True)
    with (
        patch("omnimex.tray.is_server_healthy", return_value=True),
        patch("omnimex.tray.open_app_window", return_value=True) as mock_open,
    ):
        rc = controller.run()
        assert rc == 0
        mock_open.assert_called_once_with("http://127.0.0.1:18999")


def test_tray_controller_background_server_start_and_stop(tmp_path: Path) -> None:
    port = _free_port()
    cfg = OmnimexConfig.model_validate(
        {
            "root_dir": tmp_path,
            "storage": {"db_path": "data/omnimex.db", "memories_dir": "data/memories"},
            "embedding": {"provider": "offline_hash", "allow_download": False},
            "adapters": {"watch_enabled": False, "scan_on_startup": False, "enabled": []},
        }
    )
    controller = OmnimexTrayController(
        cfg,
        port=port,
        scan_on_startup=False,
        watch=False,
        open_on_start=False,
    )
    controller.start_server_background()
    try:
        assert wait_for_server(controller.url, timeout=8.0) is True
        controller.icon = MagicMock()
        controller.trigger_scan_async()
    finally:
        controller.stop_server(timeout=5.0)
