"""FastAPI REST and SSE route definitions for Omnimex."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, status
from fastapi.responses import StreamingResponse

from omnimex import __version__
from omnimex.api.schemas import (
    AdapterHealthSchema,
    AdaptersResponse,
    ApiError,
    BackfillRequest,
    BackfillResponse,
    DistillerPatchRequest,
    DistillerSettingsResponse,
    HandoffRequest,
    HealthResponse,
    MemoriesListResponse,
    MemoryCreateRequest,
    MemoryDeleteResponse,
    MemoryPatchRequest,
    MemorySyncRequest,
    MemorySyncResponse,
    NativeImportStatusSchema,
    ScanErrorSchema,
    ScanRequest,
    ScanResponse,
    SearchResponse,
    SessionDetailResponse,
    SessionsListResponse,
    SessionTurnResponse,
    StatsResponse,
)
from omnimex.core.models import (
    DistillStatus,
    HandoffBrief,
    MemoryCard,
    MemoryCategory,
    MemoryOrigin,
    NormalizedTurn,
    SearchMode,
)
from omnimex.memory.native_import import NativeImportSourceStatus
from omnimex.services.ingest import (
    IngestService,
    ServerEvent,
    format_sse_frame,
)

router = APIRouter(prefix="/api", tags=["omnimex"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _service(request: Request) -> IngestService:
    return request.app.state.ingest_service


def _split_csv(raw: str | None) -> list[str] | None:
    if not raw or not raw.strip():
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items or None


def _wants_sse(request: Request, stream: bool) -> bool:
    if stream:
        return True
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept.lower()


def _map_native_statuses(
    items: list[NativeImportSourceStatus],
) -> list[NativeImportStatusSchema]:
    allowed = {"synced", "pending", "not_found", "error"}
    mapped: list[NativeImportStatusSchema] = []
    for item in items:
        status_val = item.status if item.status in allowed else (
            "pending" if item.status == "empty" else "not_found"
        )
        mapped.append(
            NativeImportStatusSchema(
                agent=item.agent,
                source_dir=item.source_dir,
                discovered_files=item.discovered_files,
                imported_count=item.imported_count,
                last_imported_at=item.last_imported_at,
                status=status_val,  # type: ignore[arg-type]
            )
        )
    return mapped


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + f"\n\n... [truncated {len(text) - max_chars:,} chars]", True


def _truncate_turn(turn: NormalizedTurn, max_chars: int) -> tuple[NormalizedTurn, bool]:
    if max_chars <= 0:
        return turn, False

    was_truncated = False
    new_content, c_trunc = _truncate_text(turn.content, max_chars)
    was_truncated = was_truncated or c_trunc

    new_thinking = turn.thinking
    if turn.thinking:
        new_thinking, th_trunc = _truncate_text(turn.thinking, min(max_chars, 6000))
        was_truncated = was_truncated or th_trunc

    tool_limit = max(1000, max_chars // 2)
    new_tools = []
    for tc in turn.tool_calls:
        new_args = tc.arguments
        if isinstance(tc.arguments, dict):
            raw_args = json.dumps(tc.arguments, ensure_ascii=False)
            if len(raw_args) > tool_limit:
                new_args = raw_args[:tool_limit] + f"... [truncated {len(raw_args) - tool_limit:,} chars]"
                was_truncated = True
        elif isinstance(tc.arguments, str) and len(tc.arguments) > tool_limit:
            new_args = tc.arguments[:tool_limit] + f"... [truncated {len(tc.arguments) - tool_limit:,} chars]"
            was_truncated = True

        new_output = tc.output
        if tc.output:
            new_output, out_trunc = _truncate_text(tc.output, tool_limit)
            was_truncated = was_truncated or out_trunc

        if new_args is not tc.arguments or new_output is not tc.output:
            new_tools.append(tc.model_copy(update={"arguments": new_args, "output": new_output}))
        else:
            new_tools.append(tc)

    if not was_truncated:
        return turn, False
    return (
        turn.model_copy(
            update={
                "content": new_content,
                "thinking": new_thinking,
                "tool_calls": new_tools,
            }
        ),
        True,
    )


async def _stream_job_sse(
    service: IngestService,
    *,
    topics: list[str],
    run_fn: Callable[[], None],
) -> StreamingResponse:
    """Run a background operation and stream its scoped SSE events until completion."""
    loop = asyncio.get_running_loop()
    sub_queue, unsubscribe = service.events.subscribe_async(loop, topics=topics)

    def _worker() -> None:
        try:
            run_fn()
        except Exception as exc:  # noqa: BLE001
            service.events.publish("error", {"message": str(exc)})
        finally:
            loop.call_soon_threadsafe(service.events._enqueue_async_drop_oldest, sub_queue, None)

    async def _event_iterator() -> AsyncIterator[str]:
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        try:
            while True:
                item: ServerEvent | None = await sub_queue.get()
                if item is None:
                    break
                yield item.to_sse()
        finally:
            unsubscribe()
            thread.join(timeout=1.0)

    return StreamingResponse(
        _event_iterator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    """Return service liveness, SQLite readiness, and active distiller provider."""
    svc = _service(request)
    ready = svc.db_ready()
    return HealthResponse(
        status="ok" if ready else "degraded",
        version=__version__,
        db_ready=ready,
        distiller_provider=svc.config.distiller.provider,
    )


@router.get("/search", response_model=SearchResponse)
def search_sessions(
    request: Request,
    q: str = "",
    agent: str | None = None,
    workspace: str | None = None,
    mode: SearchMode = "hybrid",
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SearchResponse:
    """Search indexed session turns via hybrid, FTS5, or semantic vector retrieval."""
    svc = _service(request)
    page = svc.searcher.search_page(
        q,
        agents=_split_csv(agent),
        workspace=workspace,
        date_from=date_from,
        date_to=date_to,
        mode=mode,
        limit=limit,
        offset=offset,
    )
    return SearchResponse(
        query=page.query,
        mode=page.mode,
        total=page.total,
        took_ms=page.took_ms,
        hits=page.hits,
        facets={
            "agents": page.facets.get("agents", {}),
            "workspaces": page.facets.get("workspaces", {}),
        },
    )


@router.get("/sessions", response_model=SessionsListResponse)
def list_sessions(
    request: Request,
    agent: str | None = None,
    workspace: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    distill_status: DistillStatus | None = None,
    include_subagents: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SessionsListResponse:
    """List ingested coding-agent session metadata with optional filtering."""
    svc = _service(request)
    sessions, total = svc.db.list_sessions(
        agents=_split_csv(agent),
        workspace=workspace,
        query=q,
        date_from=date_from,
        date_to=date_to,
        distill_status=distill_status,
        include_subagents=include_subagents,
        limit=limit,
        offset=offset,
    )
    return SessionsListResponse(
        total=total,
        limit=limit,
        offset=offset,
        sessions=sessions,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    request: Request,
    session_id: str,
    turn_offset: int | None = Query(default=None, ge=0),
    turn_limit: int = Query(default=40, ge=1, le=200),
    max_turn_chars: int = Query(default=12000, ge=0, le=500000),
    qa_only: bool = Query(default=False),
) -> SessionDetailResponse:
    """Fetch paginated session turns, collapsed/bounded oversized payloads, and linked memories."""
    svc = _service(request)
    session = svc.db.get_session(session_id, include_turns=False)
    if session is None:
        raise ApiError(404, "NOT_FOUND", f"Session '{session_id}' was not found")

    raw_turns, total_turns, effective_offset = svc.db.get_session_turns_window(
        session_id,
        offset=turn_offset,
        limit=turn_limit,
        tail=(turn_offset is None),
        qa_only=qa_only,
    )
    trimmed_turns: list[NormalizedTurn] = []
    truncated_indices: list[int] = []
    for t in raw_turns:
        trimmed, was_trunc = _truncate_turn(t, max_turn_chars)
        trimmed_turns.append(trimmed)
        if was_trunc:
            truncated_indices.append(t.turn_index)

    linked = svc.memory_manager.list_memories(source_session_id=session_id, limit=200)
    hydrated = session.model_copy(
        update={
            "turns": trimmed_turns,
            "turn_count": max(session.turn_count, total_turns),
        }
    )
    return SessionDetailResponse(
        session=hydrated,
        linked_memories=linked,
        total_turns=total_turns,
        turn_offset=effective_offset,
        turn_limit=turn_limit,
        has_earlier_turns=effective_offset > 0,
        has_later_turns=(effective_offset + len(trimmed_turns)) < total_turns,
        truncated_turn_indices=truncated_indices,
    )


@router.get("/sessions/{session_id}/turns/{turn_index}", response_model=SessionTurnResponse)
def get_session_turn_full(
    request: Request,
    session_id: str,
    turn_index: int,
) -> SessionTurnResponse:
    """Fetch a single full, untruncated turn (used when expanding oversized turns in the UI)."""
    svc = _service(request)
    turn = svc.db.get_session_turn(session_id, turn_index)
    if turn is None:
        raise ApiError(
            404,
            "NOT_FOUND",
            f"Turn #{turn_index} for session '{session_id}' was not found",
        )
    return SessionTurnResponse(turn=turn, truncated=False)


@router.get("/memories", response_model=MemoriesListResponse)
def list_memories(
    request: Request,
    category: MemoryCategory | None = None,
    distill_status: DistillStatus | None = None,
    origin: MemoryOrigin | None = None,
    reviewed: bool | None = None,
    workspace: str | None = None,
    agent: str | None = None,
    q: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> MemoriesListResponse:
    """List central Markdown memories along with category/status summaries."""
    svc = _service(request)
    all_matching = svc.memory_manager.list_memories(
        category=category,
        distill_status=distill_status,
        origin=origin,
        reviewed=reviewed,
        workspace=workspace,
        agent=agent,
        query=q,
        limit=5000,
        offset=0,
    )
    db_stats = svc.db.get_stats()
    return MemoriesListResponse(
        total=len(all_matching),
        counts_by_category=db_stats["memories_by_category"],
        counts_by_status=db_stats["memories_by_status"],
        native_imports=_map_native_statuses(svc.get_native_import_statuses()),
        memories=all_matching[offset : offset + limit],
    )


@router.post("/memories", response_model=MemoryCard, status_code=status.HTTP_201_CREATED)
def create_memory(request: Request, payload: MemoryCreateRequest) -> MemoryCard:
    """Create a central Markdown memory card and index it in SQLite."""
    svc = _service(request)
    title = payload.title.strip()
    content = payload.content.strip()
    if not title or not content:
        raise ApiError(422, "VALIDATION_ERROR", "Memory title and content must be non-empty")

    card = svc.memory_manager.create_memory(
        title=title,
        category=payload.category,
        content=content,
        tags=payload.tags,
        workspace=payload.workspace,
        distill_status="heuristic",
        origin="manual",
        reviewed=payload.reviewed,
        source_session_id=payload.source_session_id,
        source_agent=payload.source_agent,
        confidence=1.0,
    )
    svc.events.publish(
        "memory.created",
        {"id": card.id, "title": card.title, "category": card.category},
    )
    return card


@router.patch("/memories/{memory_id}", response_model=MemoryCard)
def patch_memory(
    request: Request,
    memory_id: str,
    payload: MemoryPatchRequest,
) -> MemoryCard:
    """Update a central memory card's metadata, content, category, or review status."""
    svc = _service(request)
    updated = svc.memory_manager.update_memory(
        memory_id,
        title=payload.title.strip() if payload.title is not None else None,
        category=payload.category,
        content=payload.content,
        tags=payload.tags,
        workspace=payload.workspace,
        reviewed=payload.reviewed,
        distill_status=payload.distill_status,
    )
    if updated is None:
        raise ApiError(404, "NOT_FOUND", f"Memory '{memory_id}' was not found")
    svc.events.publish(
        "memory.updated",
        {"id": updated.id, "reviewed": updated.reviewed, "category": updated.category},
    )
    return updated


@router.delete("/memories/{memory_id}", response_model=MemoryDeleteResponse)
def delete_memory(request: Request, memory_id: str) -> MemoryDeleteResponse:
    """Delete a memory card from SQLite and remove its central Markdown file."""
    svc = _service(request)
    if not svc.memory_manager.delete_memory(memory_id):
        raise ApiError(404, "NOT_FOUND", f"Memory '{memory_id}' was not found")
    svc.events.publish("memory.updated", {"id": memory_id, "deleted": True})
    return MemoryDeleteResponse(deleted=True, id=memory_id)


@router.post("/memories/sync", response_model=MemorySyncResponse)
def sync_memories(
    request: Request,
    payload: Annotated[MemorySyncRequest | None, Body()] = None,
) -> MemorySyncResponse:
    """Reconcile central Markdown memories on disk and import read-only native memories."""
    svc = _service(request)
    opts = payload or MemorySyncRequest()
    report = svc.sync_memories(
        import_native=opts.import_native,
        overwrite_existing=opts.overwrite_existing,
    )
    return MemorySyncResponse(
        synced_files=report.synced_files,
        added=report.added,
        updated=report.updated,
        deleted=report.deleted,
        imported_native=report.imported_native,
        native_imports=_map_native_statuses(report.native_imports),
    )


@router.post("/handoff", response_model=HandoffBrief)
def generate_handoff(request: Request, payload: HandoffRequest) -> HandoffBrief:
    """Build a redacted cross-agent handoff brief from a stored session."""
    svc = _service(request)
    return svc.handoff_builder.build(
        payload.session_id,
        target_agent=payload.target_agent,
        include_memories=payload.include_memories,
        include_tool_summary=payload.include_tool_summary,
        max_tokens=payload.max_tokens,
        custom_instructions=payload.custom_instructions,
    )


@router.get("/adapters", response_model=AdaptersResponse)
def list_adapters(request: Request) -> AdaptersResponse:
    """Report discovery health, Windows permission diagnostics, and counts per adapter."""
    svc = _service(request)
    details = svc.get_adapter_statuses()
    total_discovered = sum(item.discovered_files for item in details)
    return AdaptersResponse(
        total_discovered_sources=total_discovered,
        watcher_running=svc.watcher_running,
        adapters=[AdapterHealthSchema(**asdict(item)) for item in details],
    )


@router.post("/scan", response_model=ScanResponse)
async def trigger_scan(
    request: Request,
    payload: Annotated[ScanRequest | None, Body()] = None,
    stream: bool = Query(default=False),
):
    """Trigger incremental or full ingestion across registered adapters (JSON or SSE)."""
    svc = _service(request)
    opts = payload or ScanRequest()

    if _wants_sse(request, stream):
        return await _stream_job_sse(
            svc,
            topics=["scan", "session", "embedding", "error"],
            run_fn=lambda: svc.scan(
                adapters=opts.agents,
                full_rescan=opts.full_rescan,
                distill=opts.distill,
                defer_embeddings=opts.defer_embeddings,
            ),
        )

    res = await asyncio.to_thread(
        svc.scan,
        adapters=opts.agents,
        full_rescan=opts.full_rescan,
        distill=opts.distill,
        defer_embeddings=opts.defer_embeddings,
    )
    return ScanResponse(
        job_id=res.job_id,
        status="completed",
        scanned_files=res.scanned_files,
        new_sessions=res.new_sessions,
        new_turns=res.new_turns,
        updated_sessions=res.updated_sessions,
        errors=[
            ScanErrorSchema(agent=e.agent, path=e.path, error=e.error) for e in res.errors
        ],
        duration_ms=res.duration_ms,
    )


@router.get("/stats", response_model=StatsResponse)
def get_stats(request: Request) -> StatsResponse:
    """Return aggregated session, turn, memory, and distiller analytics."""
    svc = _service(request)
    return StatsResponse.model_validate(svc.get_stats())


@router.get("/settings/distiller", response_model=DistillerSettingsResponse)
def get_distiller_settings(request: Request) -> DistillerSettingsResponse:
    """Return active distiller settings with secret API key masked."""
    svc = _service(request)
    return DistillerSettingsResponse.model_validate(svc.get_distiller_settings())


@router.patch("/settings/distiller", response_model=DistillerSettingsResponse)
def patch_distiller_settings(
    request: Request,
    payload: DistillerPatchRequest,
) -> DistillerSettingsResponse:
    """Switch active distiller provider/model and persist local settings."""
    svc = _service(request)
    fields = payload.model_fields_set
    updated = svc.update_distiller_settings(
        provider=payload.provider,
        model=payload.model,
        model_provided="model" in fields,
        base_url=payload.base_url,
        base_url_provided="base_url" in fields,
        api_key=payload.api_key,
        api_key_provided="api_key" in fields,
        auto_distill_on_scan=payload.auto_distill_on_scan,
    )
    return DistillerSettingsResponse.model_validate(updated)


@router.post("/distill/backfill", response_model=BackfillResponse)
async def distill_backfill(
    request: Request,
    payload: Annotated[BackfillRequest | None, Body()] = None,
    stream: bool = Query(default=False),
):
    """Run batch distillation across pending sessions (JSON or SSE stream)."""
    svc = _service(request)
    opts = payload or BackfillRequest()

    if _wants_sse(request, stream):
        return await _stream_job_sse(
            svc,
            topics=["distill", "error"],
            run_fn=lambda: svc.backfill_distill(
                limit=opts.limit,
                agent=opts.agent,
                force=opts.force,
            ),
        )

    job_id, result = await asyncio.to_thread(
        svc.backfill_distill,
        limit=opts.limit,
        agent=opts.agent,
        force=opts.force,
    )
    return BackfillResponse(
        job_id=job_id,
        status="completed",
        provider=result.provider,
        processed_sessions=result.processed_sessions,
        created_memories=result.created_memories,
        skipped_sessions=result.skipped_sessions,
    )


@router.get("/events")
async def stream_events(
    request: Request,
    topics: str | None = None,
    heartbeat_seconds: float | None = Query(default=None, ge=0.05, le=120.0),
) -> StreamingResponse:
    """Unified async Server-Sent Events stream for live ingestion, memory syncs, and backfill."""
    svc = _service(request)
    loop = asyncio.get_running_loop()
    sub_queue, unsubscribe = svc.events.subscribe_async(loop, topics=_split_csv(topics))
    default_hb = float(getattr(request.app.state, "sse_heartbeat_seconds", 15.0))
    timeout_s = heartbeat_seconds if heartbeat_seconds is not None else default_hb

    async def _event_stream() -> AsyncIterator[str]:
        try:
            yield format_sse_frame(
                "connected",
                {
                    "server_time": svc.format_utc_now(),
                    "watcher_running": svc.watcher_running,
                },
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(sub_queue.get(), timeout=timeout_s)
                except TimeoutError:
                    yield format_sse_frame("heartbeat", {"ts": svc.format_utc_now()})
                    continue
                if event is None:
                    break
                yield event.to_sse()
        finally:
            unsubscribe()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
