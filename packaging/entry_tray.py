"""PyInstaller windowed GUI entrypoint for Omnimex."""

from __future__ import annotations

import multiprocessing

from omnimex.tray import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
