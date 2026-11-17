"""Debounced Watchdog file watcher for live coding-agent transcript updates."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from omnimex.adapters.base import BaseAgentAdapter, normalize_source_path, safe_is_dir
from omnimex.adapters.registry import AdapterRegistry

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_MS = 500


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """A debounced change notification resolved to an Omnimex agent adapter."""

    agent: str
    path: Path
    event_type: str
    adapter: BaseAgentAdapter


class _DebouncedAdapterHandler(FileSystemEventHandler):
    """Coalesce rapid filesystem events per canonical path before dispatching."""

    def __init__(
        self,
        registry: AdapterRegistry,
        callback: Callable[[WatchEvent], None],
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        enabled_agents: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._callback = callback
        self._debounce_s = max(debounce_ms, 10) / 1000.0
        self._enabled_agents = list(enabled_agents) if enabled_agents is not None else None
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._pending: dict[str, WatchEvent] = {}
        self._closed = False

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_path(Path(str(event.src_path)), "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_path(Path(str(event.src_path)), "modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and getattr(event, "dest_path", None):
            self._handle_path(Path(str(event.dest_path)), "modified")

    def _handle_path(self, raw_path: Path, event_type: str) -> None:
        resolved = self._registry.resolve_adapter_for_path(
            raw_path, enabled=self._enabled_agents
        )
        if resolved is None:
            return
        adapter, target_path = resolved
        key = normalize_source_path(target_path)
        watch_ev = WatchEvent(
            agent=adapter.agent_id,
            path=target_path,
            event_type=event_type,
            adapter=adapter,
        )
        with self._lock:
            if self._closed:
                return
            self._pending[key] = watch_ev
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self._debounce_s, self._fire, args=(key,))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: str) -> None:
        with self._lock:
            event = self._pending.pop(key, None)
            self._timers.pop(key, None)
            if self._closed or event is None:
                return
        try:
            self._callback(event)
        except Exception:  # noqa: BLE001
            logger.exception("Watcher callback failed for %s", event.path)

    def reopen(self) -> None:
        """Allow the debounced handler to accept events on a watcher restart."""
        with self._lock:
            self._closed = False

    def cancel_all(self) -> None:
        """Cancel all pending debounce timers cleanly."""
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()


class SessionWatcher:
    """Watch coding-agent session directories and dispatch debounced ``WatchEvent`` updates."""

    def __init__(
        self,
        registry: AdapterRegistry,
        callback: Callable[[WatchEvent], None],
        *,
        watch_paths: Sequence[str | Path] | None = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        enabled_agents: Sequence[str] | None = None,
    ) -> None:
        self._registry = registry
        self._callback = callback
        self._explicit_paths = (
            [Path(p).expanduser() for p in watch_paths] if watch_paths is not None else None
        )
        self._enabled_agents = list(enabled_agents) if enabled_agents is not None else None
        self._handler = _DebouncedAdapterHandler(
            registry,
            callback,
            debounce_ms=debounce_ms,
            enabled_agents=self._enabled_agents,
        )
        self._observer = Observer()
        self._watched: list[Path] = []
        self._running = False

    @property
    def is_running(self) -> bool:
        """Return True while the background observer thread is active."""
        return self._running

    @property
    def watched_paths(self) -> list[Path]:
        """Return directories currently scheduled on the observer."""
        return list(self._watched)

    def start(self) -> None:
        """Schedule recursive watches and start the Watchdog observer thread."""
        if self._running:
            return

        self._handler.reopen()
        self._observer = Observer()
        targets = (
            [p for p in self._explicit_paths if safe_is_dir(p)]
            if self._explicit_paths is not None
            else self._registry.watch_directories(enabled=self._enabled_agents)
        )
        self._watched.clear()
        for target in targets:
            try:
                self._observer.schedule(self._handler, str(target), recursive=True)
                self._watched.append(target)
                logger.info("Watching agent directory: %s", target)
            except (PermissionError, OSError) as exc:
                logger.warning("Cannot watch %s: %s", target, exc)

        self._observer.start()
        self._running = True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop watching and cancel any pending debounce timers."""
        if not self._running:
            self._handler.cancel_all()
            return
        self._handler.cancel_all()
        self._observer.stop()
        self._observer.join(timeout=timeout)
        self._running = False

    def __enter__(self) -> SessionWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["DEFAULT_DEBOUNCE_MS", "SessionWatcher", "WatchEvent"]
