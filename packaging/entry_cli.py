"""PyInstaller console CLI and MCP stdio entrypoint for omnimex-cli."""

from __future__ import annotations

import multiprocessing

from omnimex.cli import app

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app()
