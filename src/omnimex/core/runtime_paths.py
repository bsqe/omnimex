"""Runtime path helpers separating read-only bundled assets from writable user data."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when executing from a frozen executable (e.g., PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Return the read-only resource root (``sys._MEIPASS`` when frozen, repo root in dev)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parents[3]


def exe_dir() -> Path:
    """Return the directory housing the executable (or repo root in dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def default_user_data_dir() -> Path:
    """Return the writable per-user data directory used by frozen builds."""
    if env_home := os.getenv("OMNIMEX_HOME"):
        return Path(env_home).expanduser().resolve()
    if local_app := os.getenv("LOCALAPPDATA"):
        return (Path(local_app) / "Omnimex").resolve()
    return (Path.home() / ".omnimex").resolve()


__all__ = [
    "bundle_dir",
    "default_user_data_dir",
    "exe_dir",
    "is_frozen",
]
