"""Windows windowless system-tray runner and standalone app-window launcher for Omnimex."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from PIL import Image, ImageDraw

from omnimex.api.app import _resolve_static_dist_dir, create_app
from omnimex.core.config import OmnimexConfig, load_config
from omnimex.services.ingest import IngestService

if TYPE_CHECKING:
    import pystray

logger = logging.getLogger(__name__)


def ensure_windowless_stdio(log_path: Path) -> None:
    """Redirect None stdout/stderr (common under pythonw/gui-scripts) to a local log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file


def create_tray_image(size: int = 64) -> Image.Image:
    """Render a crisp Omnimex hub badge icon in memory."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(2, size // 16)
    radius = max(8, size // 4)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=(15, 23, 42, 255),
        outline=(99, 102, 241, 255),
        width=max(2, size // 16),
    )

    ring_pad = size // 4
    draw.ellipse(
        [ring_pad, ring_pad, size - ring_pad, size - ring_pad],
        outline=(56, 189, 248, 255),
        width=max(3, size // 10),
    )

    dot_r = max(3, size // 10)
    cx = cy = size // 2
    draw.ellipse(
        [cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
        fill=(129, 140, 248, 255),
    )
    return img


def ensure_tray_ico(ico_path: Path) -> Path:
    """Save multi-size .ico icon for Windows shortcuts if missing."""
    if not ico_path.is_file():
        ico_path.parent.mkdir(parents=True, exist_ok=True)
        base = create_tray_image(128)
        base.save(
            ico_path,
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)],
        )
    return ico_path


def find_app_browser_command(url: str) -> list[str] | None:
    """Locate Edge or Chrome on Windows to open Omnimex in frameless --app mode."""
    candidates: list[Path] = []
    for name in ("msedge", "msedge.exe", "chrome", "chrome.exe"):
        if found := shutil.which(name):
            candidates.append(Path(found))

    if sys.platform == "win32":
        prog_x86 = os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        prog = os.getenv("PROGRAMFILES", r"C:\Program Files")
        local_app = os.getenv("LOCALAPPDATA", "")
        candidates.extend(
            [
                Path(prog_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(prog) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(prog) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(prog_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
        )
        if local_app:
            candidates.append(
                Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )

    for exe in candidates:
        if exe.is_file():
            return [str(exe), f"--app={url}"]
    return None


def build_app_launch_url(base_url: str, config: OmnimexConfig | None = None) -> str:
    """Append a deterministic frontend bundle hash so Edge/Chrome --app windows bypass stale cache."""
    clean = base_url.rstrip("/")
    dist_dir = _resolve_static_dist_dir(config) if config is not None else None
    if dist_dir is None:
        candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
        if candidate.is_dir():
            dist_dir = candidate
    if dist_dir is not None:
        index_file = dist_dir / "index.html"
        if index_file.is_file():
            try:
                digest = hashlib.sha256(index_file.read_bytes()).hexdigest()[:8]
                return f"{clean}/?v={digest}"
            except OSError:
                pass
    return clean


def open_app_window(url: str) -> bool:
    """Open Omnimex in a standalone Edge/Chrome --app window, falling back to browser."""
    cmd = find_app_browser_command(url)
    if cmd is not None:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            subprocess.Popen(cmd, **kwargs)
            return True
        except OSError as exc:
            logger.warning("Failed to launch app browser %s: %s", cmd[0], exc)
    return bool(webbrowser.open(url))


def is_server_healthy(url: str, timeout: float = 0.8) -> bool:
    """Return True if an Omnimex server is already responding at *url*."""
    try:
        resp = httpx.get(f"{url.rstrip('/')}/api/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def wait_for_server(url: str, *, timeout: float = 10.0) -> bool:
    """Wait until Omnimex /api/health answers 200 or *timeout* expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_server_healthy(url, timeout=0.5):
            return True
        time.sleep(0.15)
    return False


class OmnimexTrayController:
    """Coordinates Uvicorn background server, tray icon, and standalone app window."""

    def __init__(
        self,
        config: OmnimexConfig,
        *,
        host: str | None = None,
        port: int | None = None,
        scan_on_startup: bool | None = None,
        watch: bool | None = None,
        open_on_start: bool = True,
    ) -> None:
        self.config = config
        self.host = host or config.app.host
        self.port = port or config.app.port
        self.scan_on_startup = scan_on_startup
        self.watch = watch
        self.open_on_start = open_on_start
        self.url = f"http://{self.host}:{self.port}"

        self.service: IngestService | None = None
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None

    def start_server_background(self) -> None:
        """Start Uvicorn in a daemon thread with non-blocking startup scan & async SSE."""
        self.service = IngestService(self.config)
        app = create_app(
            self.config,
            ingest_service=self.service,
            serve_static=True,
            start_watcher=self.watch,
            scan_on_startup=self.scan_on_startup,
        )
        uv_cfg = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level=self.config.app.log_level,
            log_config=None,
        )
        self.server = uvicorn.Server(uv_cfg)
        self.server_thread = threading.Thread(
            target=self.server.run,
            name="omnimex-uvicorn",
            daemon=True,
        )
        self.server_thread.start()

    def stop_server(self, timeout: float = 4.0) -> None:
        """Gracefully signal Uvicorn & SSE streams to stop and close DB resources."""
        if self.service is not None:
            self.service.events.close()
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None and self.server_thread.is_alive():
            self.server_thread.join(timeout=timeout)
        if self.service is not None:
            self.service.close()
            self.service = None

    def _notify(self, title: str, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:  # noqa: BLE001
                logger.debug("Tray notify skipped: %s - %s", title, message)

    def trigger_scan_async(self) -> None:
        if self.service is None:
            return
        svc = self.service

        def _run() -> None:
            try:
                res = svc.scan()
                self._notify(
                    "Omnimex 扫描完成",
                    f"扫描 {res.scanned_files} 个文件 · 新增 {res.new_sessions} 会话 / {res.new_turns} 轮次",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tray scan failed")
                self._notify("Omnimex 扫描失败", str(exc))

        threading.Thread(target=_run, name="omnimex-tray-scan", daemon=True).start()

    def toggle_watcher(self) -> None:
        if self.service is None:
            return
        if self.service.watcher_running:
            self.service.stop_watcher()
            self._notify("Omnimex", "已暂停后台文件监听 (Watcher)")
        else:
            self.service.start_watcher()
            self._notify("Omnimex", "已开启后台文件监听 (Watcher)")

    def run(self) -> int:
        """Start Omnimex server (or reuse existing) and run the system tray event loop."""
        ensure_windowless_stdio(self.config.root_dir / "data" / "omnimex-tray.log")
        ensure_tray_ico(self.config.root_dir / "data" / "omnimex.ico")

        launch_url = build_app_launch_url(self.url, self.config)

        if is_server_healthy(self.url):
            if self.open_on_start:
                open_app_window(launch_url)
            return 0

        import pystray

        self.start_server_background()
        wait_for_server(self.url, timeout=8.0)

        if self.open_on_start:
            open_app_window(launch_url)

        menu = pystray.Menu(
            pystray.MenuItem(
                "打开 Omnimex 独立窗口",
                lambda _icon, _item: open_app_window(build_app_launch_url(self.url, self.config)),
                default=True,
            ),
            pystray.MenuItem(
                "在默认浏览器打开",
                lambda _icon, _item: webbrowser.open(build_app_launch_url(self.url, self.config)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "立即增量扫描 (Scan Now)",
                lambda _icon, _item: self.trigger_scan_async(),
            ),
            pystray.MenuItem(
                "切换实时监听 (Watcher)",
                lambda _icon, _item: self.toggle_watcher(),
                checked=lambda _item: bool(self.service and self.service.watcher_running),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "退出 Omnimex",
                lambda icon, _item: icon.stop(),
            ),
        )

        self.icon = pystray.Icon(
            "omnimex",
            create_tray_image(64),
            f"Omnimex Hub ({self.url})",
            menu,
        )
        try:
            self.icon.run()
        finally:
            self.stop_server()
        return 0


def run_tray(
    *,
    config: OmnimexConfig | None = None,
    host: str | None = None,
    port: int | None = None,
    scan_on_startup: bool | None = None,
    watch: bool | None = None,
    open_on_start: bool = True,
) -> int:
    """Launch the Omnimex Windows tray controller."""
    cfg = config or load_config()
    controller = OmnimexTrayController(
        cfg,
        host=host,
        port=port,
        scan_on_startup=scan_on_startup,
        watch=watch,
        open_on_start=open_on_start,
    )
    return controller.run()


def main() -> None:
    """GUI script entrypoint for ``omnimex-tray.exe``."""
    repo_cfg = Path(__file__).resolve().parents[2] / "omnimex.toml"
    cfg = load_config(repo_cfg if repo_cfg.is_file() else None)
    raise SystemExit(run_tray(config=cfg))


__all__ = [
    "OmnimexTrayController",
    "build_app_launch_url",
    "create_tray_image",
    "ensure_tray_ico",
    "ensure_windowless_stdio",
    "find_app_browser_command",
    "is_server_healthy",
    "main",
    "open_app_window",
    "run_tray",
    "wait_for_server",
]
