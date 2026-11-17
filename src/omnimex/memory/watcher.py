"""Watchdog file watcher for central Markdown memory files."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from omnimex.memory.markdown_store import MemoryManager

logger = logging.getLogger(__name__)


class _MemoryMarkdownHandler(FileSystemEventHandler):
    """Debounced Watchdog handler syncing changed ``.md`` files into OmnimexDB."""

    def __init__(
        self,
        manager: MemoryManager,
        *,
        debounce_ms: int = 300,
        on_sync: Callable[[str, Path], None] | None = None,
    ) -> None:
        self._manager = manager
        self._debounce_s = max(0.05, debounce_ms / 1000.0)
        self._on_sync = on_sync
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._pending: dict[str, str] = {}

    @staticmethod
    def _is_markdown(path: str) -> bool:
        return Path(path).suffix.lower() == ".md"

    def _schedule(self, event_type: str, path: str) -> None:
        if not self._is_markdown(path):
            return
        with self._lock:
            self._pending[path] = event_type
            if prev := self._timers.get(path):
                prev.cancel()
            timer = threading.Timer(self._debounce_s, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            event_type = self._pending.pop(path, None)
            self._timers.pop(path, None)
        if not event_type:
            return
        target = Path(path)
        try:
            self._manager.sync_file(target)
            if self._on_sync is not None:
                self._on_sync(event_type, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync memory file %s (%s): %s", target, event_type, exc)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("created", str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("modified", str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("deleted", str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("deleted", str(event.src_path))
            if hasattr(event, "dest_path"):
                self._schedule("created", str(event.dest_path))

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()


class CentralMemoryWatcher:
    """Monitors ``memories_dir`` and keeps SQLite synced with Markdown edits."""

    def __init__(
        self,
        manager: MemoryManager,
        *,
        debounce_ms: int = 300,
        on_sync: Callable[[str, Path], None] | None = None,
    ) -> None:
        self._manager = manager
        self._handler = _MemoryMarkdownHandler(
            manager, debounce_ms=debounce_ms, on_sync=on_sync
        )
        self._observer: Observer | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._manager.memories_dir.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(self._handler, str(self._manager.memories_dir), recursive=True)
        observer.start()
        self._observer = observer
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        self._handler.cancel_all()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
        self._running = False

    def __enter__(self) -> CentralMemoryWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["CentralMemoryWatcher"]
