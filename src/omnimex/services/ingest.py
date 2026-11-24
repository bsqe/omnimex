"""Orchestration service for incremental ingestion, live watching, distillation, and stats."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from omnimex.adapters.base import BaseAgentAdapter, HealthState, normalize_source_path
from omnimex.adapters.registry import AdapterRegistry, create_default_registry
from omnimex.adapters.watcher import SessionWatcher, WatchEvent
from omnimex.core.config import (
    DistillerConfig,
    OmnimexConfig,
    load_config,
    save_local_distiller_config,
)
from omnimex.core.models import (
    SavedSkillSummary,
    SkillDraft,
    SkillInstallResult,
    SkillInstallTarget,
    utc_now,
)
from omnimex.distill.pipeline import BackfillResult, DistillationPipeline
from omnimex.handoff.builder import HandoffBuilder
from omnimex.memory.markdown_store import MemoryManager, MemorySyncReport
from omnimex.memory.native_import import NativeImportSourceStatus, NativeMemoryImporter
from omnimex.skill_forge import (
    SkillBuilder,
    build_skill_zip_bytes,
    delete_saved_skill,
    install_packaged_skill,
    list_saved_skills,
)
from omnimex.skill_forge import (
    save_skill as archive_save_skill,
)
from omnimex.storage.db import OmnimexDB, format_iso_utc
from omnimex.storage.embeddings import EmbeddingProvider, create_embedder
from omnimex.storage.search import HybridSearcher

logger = logging.getLogger(__name__)


_QueueItemT = TypeVar("_QueueItemT")


def enqueue_drop_oldest(q: queue.Queue[_QueueItemT], item: _QueueItemT) -> bool:
    """Enqueue an item onto a bounded queue, dropping the oldest item if full."""
    try:
        q.put_nowait(item)
        return True
    except queue.Full:
        with contextlib.suppress(queue.Empty):
            q.get_nowait()
        try:
            q.put_nowait(item)
            return True
        except queue.Full:
            return False


def format_sse_frame(event_type: str, data: dict[str, Any]) -> str:
    """Serialize an event name and JSON payload into an SSE frame."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


@dataclass(frozen=True, slots=True)
class ServerEvent:
    """Structured event published to connected SSE subscribers."""

    event: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        """Render this event as a wire-ready SSE message block."""
        return format_sse_frame(self.event, self.data)


class EventBroker:
    """Thread-safe in-process pub/sub hub for Omnimex sync & async SSE streams."""

    def __init__(self, *, max_queue_size: int = 256) -> None:
        self._max_queue_size = max(16, max_queue_size)
        self._lock = threading.Lock()
        self._subscribers: dict[int, tuple[set[str] | None, queue.Queue[ServerEvent | None]]] = {}
        self._async_subscribers: dict[
            int,
            tuple[
                set[str] | None,
                asyncio.AbstractEventLoop,
                asyncio.Queue[ServerEvent | None],
            ],
        ] = {}
        self._next_id = 1
        self._closed = False

    def subscribe(
        self,
        topics: Sequence[str] | None = None,
    ) -> tuple[queue.Queue[ServerEvent | None], Callable[[], None]]:
        """Register a sync subscriber queue optionally filtered by top-level event topics."""
        topic_set = {t.strip().lower() for t in topics if t and t.strip()} if topics else None
        q: queue.Queue[ServerEvent | None] = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            if self._closed:
                q.put_nowait(None)
                return q, (lambda: None)
            sub_id = self._next_id
            self._next_id += 1
            self._subscribers[sub_id] = (topic_set, q)

        def _unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(sub_id, None)

        return q, _unsubscribe

    def subscribe_async(
        self,
        loop: asyncio.AbstractEventLoop,
        topics: Sequence[str] | None = None,
    ) -> tuple[asyncio.Queue[ServerEvent | None], Callable[[], None]]:
        """Register an asyncio subscriber queue woken via ``call_soon_threadsafe``."""
        topic_set = {t.strip().lower() for t in topics if t and t.strip()} if topics else None
        aq: asyncio.Queue[ServerEvent | None] = asyncio.Queue(maxsize=self._max_queue_size)
        with self._lock:
            if self._closed:
                aq.put_nowait(None)
                return aq, (lambda: None)
            sub_id = self._next_id
            self._next_id += 1
            self._async_subscribers[sub_id] = (topic_set, loop, aq)

        def _unsubscribe() -> None:
            with self._lock:
                self._async_subscribers.pop(sub_id, None)

        return aq, _unsubscribe

    @staticmethod
    def _enqueue_async_drop_oldest(
        aq: asyncio.Queue[ServerEvent | None],
        item: ServerEvent | None,
    ) -> None:
        try:
            aq.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                aq.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                aq.put_nowait(item)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all matching SSE subscribers without blocking callers."""
        topic = event_type.split(".", 1)[0].lower()
        event = ServerEvent(event=event_type, data=data)
        with self._lock:
            if self._closed:
                return
            targets = [
                q
                for topic_set, q in self._subscribers.values()
                if topic_set is None or topic in topic_set
            ]
            async_targets = [
                (loop, aq)
                for topic_set, loop, aq in self._async_subscribers.values()
                if topic_set is None or topic in topic_set
            ]
        for q in targets:
            if not enqueue_drop_oldest(q, event):
                logger.debug("Dropped SSE event %s for slow subscriber", event_type)
        for loop, aq in async_targets:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._enqueue_async_drop_oldest, aq, event)

    def reopen(self) -> None:
        """Re-enable event publishing and subscriptions after a previous shutdown."""
        with self._lock:
            self._closed = False

    def close(self) -> None:
        """Broadcast shutdown sentinel to all active SSE subscribers immediately."""
        with self._lock:
            self._closed = True
            sync_targets = [q for _, q in self._subscribers.values()]
            async_targets = [(loop, aq) for _, loop, aq in self._async_subscribers.values()]
        for q in sync_targets:
            enqueue_drop_oldest(q, None)
        for loop, aq in async_targets:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._enqueue_async_drop_oldest, aq, None)


@dataclass(frozen=True, slots=True)
class ScanError:
    """Non-fatal error encountered while scanning or parsing an adapter source."""

    agent: str
    path: str
    error: str


@dataclass(slots=True)
class ScanResult:
    """Outcome summary produced by an incremental or full adapter scan."""

    job_id: str
    status: str = "completed"
    scanned_files: int = 0
    new_sessions: int = 0
    new_turns: int = 0
    updated_sessions: int = 0
    errors: list[ScanError] = field(default_factory=list)
    duration_ms: float = 0.0
    distilled_memories: int = 0
    imported_native_memories: int = 0


@dataclass(slots=True)
class AdapterHealthDetail:
    """Combined adapter discovery health and SQLite ingestion statistics."""

    id: str
    name: str
    status: HealthState
    source_paths: list[str]
    discovered_files: int
    session_count: int
    turn_count: int
    native_memory_count: int
    last_scan_at: datetime | None
    last_error: str | None
    watcher_active: bool
    supports_native_memories: bool


class IngestService:
    """High-level coordinator uniting adapters, storage, embeddings, memory, and distiller."""

    def __init__(
        self,
        config: OmnimexConfig | None = None,
        *,
        db: OmnimexDB | None = None,
        registry: AdapterRegistry | None = None,
        embedder: EmbeddingProvider | None = None,
        memory_manager: MemoryManager | None = None,
        distillation: DistillationPipeline | None = None,
        handoff_builder: HandoffBuilder | None = None,
        skill_builder: SkillBuilder | None = None,
        native_importer: NativeMemoryImporter | None = None,
    ) -> None:
        self.config = config or load_config()
        self._owns_db = db is None
        self.db = db or OmnimexDB(
            self.config.db_path,
            wal_mode=self.config.storage.wal_mode,
            busy_timeout_ms=self.config.storage.busy_timeout_ms,
            redact=self.config.redaction.enabled,
        )
        self.registry = registry or create_default_registry(enabled=self.config.adapters.enabled)
        self.embedder = embedder or create_embedder(
            self.config.embedding,
            root_dir=self.config.root_dir,
        )
        self.memory_manager = memory_manager or MemoryManager(
            self.db,
            self.config.memories_dir,
            embedder=self.embedder,
            redact=self.config.redaction.enabled,
        )
        self.distillation = distillation or DistillationPipeline(
            self.db,
            self.memory_manager,
            config=self.config.distiller,
        )
        self.handoff_builder = handoff_builder or HandoffBuilder(
            self.db,
            self.memory_manager,
        )
        self.skill_builder = skill_builder or SkillBuilder(
            self.db,
            self.memory_manager,
            distiller_config=self.config.distiller,
        )
        self.searcher = HybridSearcher(self.db, self.embedder)
        self.events = EventBroker()
        self._native_importer = native_importer
        self._ingest_lock = threading.RLock()
        self._embed_lock = threading.Lock()
        self._embed_stop = threading.Event()
        self._embed_thread: threading.Thread | None = None
        self._embed_rescan = False
        self._watcher: SessionWatcher | None = None
        self._last_scan_at: dict[str, datetime] = {}
        self._last_native_imports: list[NativeImportSourceStatus] = []

    @property
    def watcher_running(self) -> bool:
        """Return True while the background Watchdog file watcher is running."""
        return self._watcher is not None and self._watcher.is_running

    def start_watcher(self) -> bool:
        """Start the debounced Watchdog observer if not already active."""
        if self._watcher is None:
            self._watcher = SessionWatcher(
                self.registry,
                self.handle_watch_event,
                debounce_ms=self.config.adapters.watch_debounce_ms,
                enabled_agents=self.config.adapters.enabled,
            )
        if not self._watcher.is_running:
            self._watcher.start()
        return self.watcher_running

    def stop_watcher(self, timeout: float = 5.0) -> None:
        """Stop the background Watchdog observer if active."""
        if self._watcher is not None and self._watcher.is_running:
            self._watcher.stop(timeout=timeout)

    def reopen(self) -> None:
        """Re-enable event publishing and subscriptions after a previous shutdown."""
        with self._lock:
            self._closed = False

    def stop_embedding_backfill(self, timeout: float = 2.0) -> None:
        """Signal any background vector backfill worker to stop and wait briefly."""
        self._embed_stop.set()
        with self._embed_lock:
            thread = self._embed_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._embed_lock:
            if self._embed_thread is thread and (thread is None or not thread.is_alive()):
                self._embed_thread = None

    def close(self) -> None:
        """Stop background watchers, embedding backfill, SSE streams, and owned DB resources."""
        self.events.close()
        self.stop_watcher()
        self.stop_embedding_backfill(timeout=2.0)
        if self._owns_db:
            self.db.close()

    def _build_native_importer(self) -> NativeMemoryImporter:
        """Build a NativeMemoryImporter scoped to the active registry when customized."""
        if self._native_importer is not None:
            return self._native_importer

        by_id = {adapter.agent_id: adapter for adapter in self.registry.adapters()}
        claude_adapter = by_id.get("claude_code")
        codex_adapter = by_id.get("codex")
        windsurf_adapter = by_id.get("windsurf")

        has_explicit = any(
            getattr(adapter, "_explicit_roots", None) is not None
            for adapter in (claude_adapter, codex_adapter, windsurf_adapter)
            if adapter is not None
        )
        if not has_explicit and len(by_id) >= 3:
            return NativeMemoryImporter()

        claude_roots = claude_adapter.resolve_roots() if claude_adapter is not None else []
        codex_roots = codex_adapter.resolve_roots() if codex_adapter is not None else []
        windsurf_roots = (
            [
                root
                for root in windsurf_adapter.resolve_roots()
                if root.name in {"windsurf", "memories"} or (root / "memories").is_dir()
            ]
            if windsurf_adapter is not None
            else []
        )
        return NativeMemoryImporter(
            claude_roots=claude_roots,
            codex_roots=codex_roots,
            windsurf_roots=windsurf_roots,
        )

    @staticmethod
    def _sort_paths_newest_first(paths: Sequence[Path]) -> list[Path]:
        def _mtime_ns(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        return sorted(paths, key=_mtime_ns, reverse=True)

    def _ingest_single_file(
        self,
        adapter: BaseAgentAdapter,
        file_path: Path,
        *,
        full_rescan: bool,
        should_distill: bool,
        errors: list[ScanError],
        embed: bool = True,
    ) -> tuple[int, int, int, int]:
        """Parse and store one source file incrementally, returning turn/session deltas."""
        cursor = None if full_rescan else self.db.get_cursor(file_path)
        prev_error_count = len(adapter.last_errors)
        parse_result = adapter.parse_file_incremental(file_path, cursor)

        for err_msg in adapter.last_errors[prev_error_count:]:
            errors.append(
                ScanError(
                    agent=adapter.agent_id,
                    path=normalize_source_path(file_path),
                    error=err_msg,
                )
            )

        if not parse_result.changed:
            if parse_result.cursor != cursor and cursor is not None:
                self.db.upsert_cursor(parse_result.cursor)
            return 0, 0, 0, 0

        new_sessions = 0
        updated_sessions = 0
        new_turns = 0
        distilled_memories = 0

        for session in parse_result.sessions:
            prev_session = self.db.get_session(session.id, include_turns=False)
            stored = self.db.upsert_session(
                session,
                replace_turns=not parse_result.is_append_only,
            )
            if embed:
                self.db.index_session_embeddings(stored.id, self.embedder, force=full_rescan)

            if prev_session is None:
                new_sessions += 1
                added_turns = stored.turn_count
            else:
                updated_sessions += 1
                if parse_result.is_append_only:
                    added_turns = len(session.turns)
                else:
                    added_turns = max(0, stored.turn_count - prev_session.turn_count)
            new_turns += added_turns

            if should_distill:
                try:
                    outcome = self.distillation.distill_session(stored.id)
                    distilled_memories += len(outcome.created_memories)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Auto-distill skipped for %s: %s", stored.id, exc)
                    errors.append(
                        ScanError(
                            agent=adapter.agent_id,
                            path=normalize_source_path(file_path),
                            error=f"distill_failed: {exc}",
                        )
                    )

            self.events.publish(
                "session.ingested",
                {
                    "session_id": stored.id,
                    "agent": stored.agent,
                    "title": stored.title,
                    "new_turns": added_turns,
                },
            )

        self.db.upsert_cursor(parse_result.cursor)
        return new_sessions, new_turns, updated_sessions, distilled_memories

    def run_embedding_backfill(self, *, stop_event: threading.Event | None = None) -> int:
        """Compute missing turn vector embeddings, prioritizing recent user sessions."""
        session_ids = self.db.list_session_ids_needing_embeddings(
            self.embedder.model_name,
            self.embedder.dimension,
        )
        if not session_ids:
            return 0

        total_sessions = len(session_ids)
        completed_sessions = 0
        indexed_turns = 0
        self.events.publish("embedding.started", {"pending_sessions": total_sessions})

        for sid in session_ids:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                added = self.db.index_session_embeddings(
                    sid,
                    self.embedder,
                    stop_event=stop_event,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Background embedding skipped for %s: %s", sid, exc)
                added = 0
            completed_sessions += 1
            indexed_turns += added
            self.events.publish(
                "embedding.progress",
                {
                    "session_id": sid,
                    "completed_sessions": completed_sessions,
                    "total_sessions": total_sessions,
                    "indexed_turns": indexed_turns,
                },
            )

        stopped = bool(stop_event is not None and stop_event.is_set())
        self.events.publish(
            "embedding.completed",
            {
                "completed_sessions": completed_sessions,
                "total_sessions": total_sessions,
                "indexed_turns": indexed_turns,
                "stopped": stopped,
            },
        )
        return indexed_turns

    def schedule_embedding_backfill(self) -> threading.Thread:
        """Start (or wake) the serialized background vector backfill worker."""
        with self._embed_lock:
            if self._embed_thread is not None and self._embed_thread.is_alive():
                self._embed_rescan = True
                return self._embed_thread

            self._embed_stop.clear()
            self._embed_rescan = False

            def _worker() -> None:
                while not self._embed_stop.is_set():
                    try:
                        self.run_embedding_backfill(stop_event=self._embed_stop)
                    except Exception:  # noqa: BLE001
                        logger.exception("Background embedding worker failed")
                    with self._embed_lock:
                        if self._embed_rescan and not self._embed_stop.is_set():
                            self._embed_rescan = False
                            continue
                        self._embed_thread = None
                        break

            thread = threading.Thread(
                target=_worker,
                name="omnimex-embedding-backfill",
                daemon=True,
            )
            self._embed_thread = thread
            thread.start()
            return thread

    def scan(
        self,
        adapters: Sequence[str] | None = None,
        *,
        full_rescan: bool = False,
        distill: bool | None = None,
        distill_heuristic: bool | None = None,
        import_native: bool = True,
        defer_embeddings: bool = False,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> ScanResult:
        """Scan configured adapters incrementally, index FTS5 (+ vectors), and optionally distill."""
        started = time.perf_counter()
        job_id = f"scan_{utc_now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        should_distill = (
            distill
            if distill is not None
            else (
                distill_heuristic
                if distill_heuristic is not None
                else self.config.distiller.auto_distill_on_scan
            )
        )

        with self._ingest_lock:
            enabled = list(adapters) if adapters is not None else self.config.adapters.enabled
            target_adapters = self.registry.adapters(enabled=enabled)
            agent_ids = [a.agent_id for a in target_adapters]

            self.events.publish("scan.started", {"job_id": job_id, "agents": agent_ids})

            result = ScanResult(job_id=job_id)
            discovered_pairs: list[tuple[BaseAgentAdapter, list[Path]]] = []
            for adapter in target_adapters:
                prev_err_count = len(adapter.last_errors)
                try:
                    files = self._sort_paths_newest_first(adapter.detect_sources())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Source discovery failed for %s: %s", adapter.agent_id, exc)
                    adapter.last_errors.append(str(exc))
                    files = []
                self._last_scan_at[adapter.agent_id] = utc_now()
                for err_msg in adapter.last_errors[prev_err_count:]:
                    result.errors.append(
                        ScanError(agent=adapter.agent_id, path="", error=err_msg)
                    )
                discovered_pairs.append((adapter, files))

            total_files = sum(len(files) for _, files in discovered_pairs)

            for adapter, files in discovered_pairs:
                self._last_scan_at[adapter.agent_id] = utc_now()
                for file_path in files:
                    try:
                        ns, nt, us, dm = self._ingest_single_file(
                            adapter,
                            file_path,
                            full_rescan=full_rescan,
                            should_distill=should_distill,
                            errors=result.errors,
                            embed=not defer_embeddings,
                        )
                        result.new_sessions += ns
                        result.new_turns += nt
                        result.updated_sessions += us
                        result.distilled_memories += dm
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Scan failed on %s (%s)", file_path, adapter.agent_id)
                        result.errors.append(
                            ScanError(
                                agent=adapter.agent_id,
                                path=normalize_source_path(file_path),
                                error=str(exc),
                            )
                        )
                    finally:
                        result.scanned_files += 1
                        progress_payload = {
                            "job_id": job_id,
                            "agent": adapter.agent_id,
                            "scanned_files": result.scanned_files,
                            "total_files": total_files,
                            "new_sessions": result.new_sessions,
                            "new_turns": result.new_turns,
                        }
                        self.events.publish("scan.progress", progress_payload)
                        if progress_cb is not None:
                            progress_cb(progress_payload)

            if import_native:
                sync_report = self.sync_memories(import_native=True, overwrite_existing=False)
                result.imported_native_memories = sync_report.imported_native
                for err in sync_report.errors:
                    result.errors.append(ScanError(agent="native_import", path="", error=err))

            result.duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            self.events.publish(
                "scan.completed",
                {
                    "job_id": job_id,
                    "new_sessions": result.new_sessions,
                    "new_turns": result.new_turns,
                    "errors_count": len(result.errors),
                    "duration_ms": result.duration_ms,
                    "deferred_embeddings": defer_embeddings,
                },
            )

        if defer_embeddings:
            self.schedule_embedding_backfill()
        return result

    def handle_watch_event(self, event: WatchEvent) -> ScanResult | None:
        """Process a single debounced Watchdog event incrementally."""
        with self._ingest_lock:
            started = time.perf_counter()
            errors: list[ScanError] = []
            try:
                ns, nt, us, dm = self._ingest_single_file(
                    event.adapter,
                    event.path,
                    full_rescan=False,
                    should_distill=self.config.distiller.auto_distill_on_scan,
                    errors=errors,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Watch ingest failed for %s", event.path)
                errors.append(
                    ScanError(
                        agent=event.agent,
                        path=normalize_source_path(event.path),
                        error=str(exc),
                    )
                )
                return None

            self._last_scan_at[event.agent] = utc_now()
            return ScanResult(
                job_id=f"watch_{utc_now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}",
                status="completed",
                scanned_files=1,
                new_sessions=ns,
                new_turns=nt,
                updated_sessions=us,
                errors=errors,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                distilled_memories=dm,
            )

    def sync_memories(
        self,
        *,
        import_native: bool = True,
        overwrite_existing: bool = False,
    ) -> MemorySyncReport:
        """Reconcile central Markdown memories with SQLite and emit ``memory.synced``."""
        importer = self._build_native_importer() if import_native else None
        report = self.memory_manager.sync_all(
            import_native=import_native,
            overwrite_existing=overwrite_existing,
            native_importer=importer,
        )
        if report.native_imports:
            self._last_native_imports = list(report.native_imports)
        self.events.publish(
            "memory.synced",
            {
                "synced_files": report.synced_files,
                "added": report.added,
                "updated": report.updated,
                "deleted": report.deleted,
                "imported_native": report.imported_native,
            },
        )
        return report

    def get_native_import_statuses(self) -> list[NativeImportSourceStatus]:
        """Return recent native import statuses without mutating the store."""
        if self._last_native_imports:
            return list(self._last_native_imports)
        native_result = self._build_native_importer().scan()
        return list(native_result.sources)

    def get_adapter_statuses(self) -> list[AdapterHealthDetail]:
        """Return live discovery health, DB counts, and watcher status per adapter."""
        reports = self.registry.health_report()
        watcher_running = self.watcher_running

        with self.db.read_connection() as conn:
            sess_rows = conn.execute(
                """
                SELECT agent, COUNT(*) AS session_count, COALESCE(SUM(turn_count), 0) AS turn_count
                FROM sessions
                GROUP BY agent
                """
            ).fetchall()
            mem_rows = conn.execute(
                """
                SELECT source_agent, COUNT(*) AS cnt
                FROM memories
                WHERE origin = 'native_import' AND source_agent IS NOT NULL
                GROUP BY source_agent
                """
            ).fetchall()

        counts_by_agent = {
            str(r["agent"]): (int(r["session_count"]), int(r["turn_count"])) for r in sess_rows
        }
        native_by_agent = {str(r["source_agent"]): int(r["cnt"]) for r in mem_rows}

        details: list[AdapterHealthDetail] = []
        for rep in reports:
            sess_count, turn_count = counts_by_agent.get(rep.agent, (0, 0))
            details.append(
                AdapterHealthDetail(
                    id=rep.agent,
                    name=rep.name,
                    status=rep.status,
                    source_paths=rep.source_paths,
                    discovered_files=rep.discovered_files,
                    session_count=sess_count,
                    turn_count=turn_count,
                    native_memory_count=max(native_by_agent.get(rep.agent, 0), rep.native_memory_files),
                    last_scan_at=self._last_scan_at.get(rep.agent),
                    last_error=rep.errors[-1] if rep.errors else None,
                    watcher_active=watcher_running and rep.status in {"healthy", "degraded"},
                    supports_native_memories=rep.supports_native_memories,
                )
            )
        return details

    def get_distiller_settings(self) -> dict[str, Any]:
        """Return sanitized public distiller configuration plus DB pending/distilled counts."""
        db_stats = self.db.get_stats()
        return {
            **self.config.distiller.to_public_dict(),
            "pending_sessions_count": db_stats["pending_sessions_count"],
            "distilled_memories_count": db_stats["distilled_memories_count"],
        }

    def update_distiller_settings(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        model_provided: bool = False,
        base_url: str | None = None,
        base_url_provided: bool = False,
        api_key: str | None = None,
        api_key_provided: bool = False,
        auto_distill_on_scan: bool | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Update active distiller provider settings and optionally persist local config."""
        current = self.config.distiller
        updates: dict[str, Any] = {}
        if provider is not None:
            updates["provider"] = provider
        if model_provided:
            updates["model"] = model or None
        if base_url_provided:
            updates["base_url"] = base_url or None
        if api_key_provided:
            updates["api_key"] = api_key if api_key else None
        if auto_distill_on_scan is not None:
            updates["auto_distill_on_scan"] = auto_distill_on_scan

        next_distiller: DistillerConfig = current.model_copy(update=updates)
        self.distillation.set_distiller(next_distiller)
        self.skill_builder.set_distiller_config(next_distiller)
        self.config = self.config.model_copy(update={"distiller": next_distiller})
        if persist:
            save_local_distiller_config(next_distiller, root_dir=self.config.root_dir)
        return self.get_distiller_settings()

    def backfill_distill(
        self,
        *,
        limit: int = 25,
        agent: str | None = None,
        force: bool = False,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[str, BackfillResult]:
        """Run memory distillation backfill and broadcast SSE progress events."""
        job_id = f"distill_{utc_now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"

        def _on_progress(payload: dict[str, Any]) -> None:
            event_data = {"job_id": job_id, **payload}
            self.events.publish("distill.progress", event_data)
            if progress_cb is not None:
                progress_cb(event_data)

        result = self.distillation.backfill(
            limit=limit,
            agent=agent,
            force=force,
            progress_cb=_on_progress,
        )
        self.events.publish(
            "distill.completed",
            {
                "job_id": job_id,
                "processed_sessions": result.processed_sessions,
                "created_memories": result.created_memories,
                "skipped_sessions": result.skipped_sessions,
                "provider": result.provider,
            },
        )
        return job_id, result

    def generate_skill(
        self,
        *,
        session_ids: Sequence[str] = (),
        memory_ids: Sequence[str] = (),
        name: str | None = None,
        description: str | None = None,
        custom_instructions: str | None = None,
        use_llm: bool = False,
    ) -> SkillDraft:
        """Synthesize a redacted SKILL.md draft from sessions and/or central memories."""
        return self.skill_builder.build(
            session_ids=session_ids,
            memory_ids=memory_ids,
            name=name,
            description=description,
            custom_instructions=custom_instructions,
            use_llm=use_llm,
        )

    def list_skills(self) -> list[SavedSkillSummary]:
        """List saved packaged skills from the central ``data/skills`` store."""
        return list_saved_skills(self.config.skills_dir)

    def save_skill(self, slug: str, skill_md: str) -> SavedSkillSummary:
        """Save (or overwrite) a packaged SKILL.md in the central store."""
        saved = archive_save_skill(self.config.skills_dir, slug, skill_md)
        self.events.publish(
            "skill.saved",
            {"slug": saved.slug, "file_path": saved.file_path},
        )
        return saved

    def delete_skill(self, slug: str) -> bool:
        """Delete a curated skill from the central store."""
        return delete_saved_skill(self.config.skills_dir, slug)

    def export_skill_zip(self, slug: str, skill_md: str) -> tuple[str, bytes]:
        """Return ``(clean_slug, zip_bytes)`` packaging ``<slug>/SKILL.md``."""
        return build_skill_zip_bytes(slug, skill_md)

    def install_skill(
        self,
        slug: str,
        skill_md: str,
        *,
        targets: Sequence[SkillInstallTarget] = (),
        custom_dir: Path | None = None,
        home_dir: Path | None = None,
        save_to_archive: bool = True,
    ) -> SkillInstallResult:
        """Install packaged SKILL.md into selected coding-agent directories."""
        if save_to_archive:
            archive_save_skill(self.config.skills_dir, slug, skill_md)
        result = install_packaged_skill(
            slug,
            skill_md,
            targets=targets,
            custom_dir=custom_dir,
            home_dir=home_dir,
        )
        self.events.publish(
            "skill.installed",
            {
                "slug": result.slug,
                "targets": [entry.target for entry in result.installed],
                "errors_count": len(result.errors),
            },
        )
        return result

    def get_stats(self) -> dict[str, Any]:
        """Return enriched telemetry matching ``GET /api/stats`` in API_CONTRACT."""
        db_stats = self.db.get_stats()
        names = {a.agent_id: a.display_name for a in self.registry.adapters()}

        with self.db.read_connection() as conn:
            native_rows = conn.execute(
                """
                SELECT source_agent, COUNT(*) AS cnt
                FROM memories
                WHERE origin = 'native_import' AND source_agent IS NOT NULL
                GROUP BY source_agent
                """
            ).fetchall()
            activity_rows = conn.execute(
                """
                SELECT substr(COALESCE(updated_at, created_at), 1, 10) AS day,
                       COUNT(*) AS sessions,
                       COALESCE(SUM(turn_count), 0) AS turns
                FROM sessions
                WHERE COALESCE(updated_at, created_at) IS NOT NULL
                GROUP BY day
                ORDER BY day DESC
                LIMIT 14
                """
            ).fetchall()

        native_by_agent = {str(r["source_agent"]): int(r["cnt"]) for r in native_rows}
        by_agent = [
            {
                "agent": item["agent"],
                "name": names.get(item["agent"], str(item["agent"]).replace("_", " ").title()),
                "sessions": item["sessions"],
                "turns": item["turns"],
                "native_memories": native_by_agent.get(item["agent"], 0),
                "last_activity_at": item["last_activity_at"],
            }
            for item in db_stats["by_agent"]
        ]
        recent_activity = [
            {
                "date": str(r["day"]),
                "sessions": int(r["sessions"]),
                "turns": int(r["turns"]),
            }
            for r in reversed(activity_rows)
            if r["day"]
        ]

        return {
            "total_sessions": db_stats["total_sessions"],
            "total_turns": db_stats["total_turns"],
            "total_memories": db_stats["total_memories"],
            "total_native_memories": db_stats["total_native_memories"],
            "db_size_bytes": db_stats["db_size_bytes"],
            "index_embeddings_count": db_stats["index_embeddings_count"],
            "by_agent": by_agent,
            "memories_by_category": db_stats["memories_by_category"],
            "memories_by_status": db_stats["memories_by_status"],
            "recent_activity": recent_activity,
            "distiller": self.get_distiller_settings(),
        }

    def db_ready(self) -> bool:
        """Return True if SQLite answers a lightweight health probe."""
        try:
            with self.db.read_connection() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def format_utc_now() -> str:
        """Return current UTC ISO-8601 timestamp with trailing ``Z``."""
        return format_iso_utc(datetime.now(UTC)) or ""
