"""Pydantic request, response, and error schemas for the Omnimex HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from omnimex.adapters.base import HealthState
from omnimex.core.config import DistillerProviderKind
from omnimex.core.models import (
    DistillStatus,
    MemoryCard,
    MemoryCategory,
    NormalizedSession,
    NormalizedTurn,
    SearchHit,
    SearchMode,
)


class ApiError(Exception):
    """Structured domain exception mapped to the standard Omnimex error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db_ready: bool
    distiller_provider: str


class SearchFacets(BaseModel):
    agents: dict[str, int] = Field(default_factory=dict)
    workspaces: dict[str, int] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    total: int
    took_ms: float
    hits: list[SearchHit] = Field(default_factory=list)
    facets: SearchFacets = Field(default_factory=SearchFacets)


class SessionsListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    sessions: list[NormalizedSession] = Field(default_factory=list)


class SessionDetailResponse(BaseModel):
    session: NormalizedSession
    linked_memories: list[MemoryCard] = Field(default_factory=list)
    total_turns: int = 0
    turn_offset: int = 0
    turn_limit: int = 40
    has_earlier_turns: bool = False
    has_later_turns: bool = False
    truncated_turn_indices: list[int] = Field(default_factory=list)


class SessionTurnResponse(BaseModel):
    turn: NormalizedTurn
    truncated: bool = False


class NativeImportStatusSchema(BaseModel):
    agent: str
    source_dir: str
    discovered_files: int = 0
    imported_count: int = 0
    last_imported_at: datetime | None = None
    status: Literal["synced", "pending", "not_found", "error"] = "not_found"


class MemoriesListResponse(BaseModel):
    total: int
    counts_by_category: dict[MemoryCategory, int]
    counts_by_status: dict[Literal["heuristic", "llm_distilled", "pending", "manual"], int]
    native_imports: list[NativeImportStatusSchema] = Field(default_factory=list)
    memories: list[MemoryCard] = Field(default_factory=list)


class MemoryCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    category: MemoryCategory
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    workspace: str | None = None
    source_session_id: str | None = None
    source_agent: str | None = None
    reviewed: bool = True


class MemoryPatchRequest(BaseModel):
    title: str | None = None
    category: MemoryCategory | None = None
    content: str | None = None
    tags: list[str] | None = None
    workspace: str | None = None
    reviewed: bool | None = None
    distill_status: DistillStatus | None = None


class MemoryDeleteResponse(BaseModel):
    deleted: bool
    id: str


class MemorySyncRequest(BaseModel):
    import_native: bool = True
    overwrite_existing: bool = False


class MemorySyncResponse(BaseModel):
    synced_files: int
    added: int
    updated: int
    deleted: int
    imported_native: int
    native_imports: list[NativeImportStatusSchema] = Field(default_factory=list)


class HandoffRequest(BaseModel):
    session_id: str = Field(min_length=1)
    target_agent: str = "generic"
    include_memories: bool = True
    include_tool_summary: bool = True
    max_tokens: int = Field(default=1200, ge=100, le=16000)
    custom_instructions: str | None = None


class AdapterHealthSchema(BaseModel):
    id: str
    name: str
    status: HealthState
    source_paths: list[str] = Field(default_factory=list)
    discovered_files: int = 0
    session_count: int = 0
    turn_count: int = 0
    native_memory_count: int = 0
    last_scan_at: datetime | None = None
    last_error: str | None = None
    watcher_active: bool = False
    supports_native_memories: bool = False


class AdaptersResponse(BaseModel):
    total_discovered_sources: int
    watcher_running: bool
    adapters: list[AdapterHealthSchema] = Field(default_factory=list)


class ScanRequest(BaseModel):
    agents: list[str] | None = None
    full_rescan: bool = False
    distill: bool | None = None


class ScanErrorSchema(BaseModel):
    agent: str
    path: str
    error: str


class ScanResponse(BaseModel):
    job_id: str
    status: Literal["started", "completed"] = "completed"
    scanned_files: int
    new_sessions: int
    new_turns: int
    updated_sessions: int
    errors: list[ScanErrorSchema] = Field(default_factory=list)
    duration_ms: float


class DistillerSettingsResponse(BaseModel):
    provider: DistillerProviderKind
    model: str | None = None
    base_url: str | None = None
    api_key_configured: bool = False
    auto_distill_on_scan: bool = True
    zero_cost_mode: bool = True
    pending_sessions_count: int = 0
    distilled_memories_count: int = 0


class DistillerPatchRequest(BaseModel):
    provider: DistillerProviderKind | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auto_distill_on_scan: bool | None = None


class BackfillRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=500)
    agent: str | None = None
    force: bool = False


class BackfillResponse(BaseModel):
    job_id: str
    status: Literal["started", "completed"] = "completed"
    provider: str
    processed_sessions: int
    created_memories: int
    skipped_sessions: int


class AgentStatSchema(BaseModel):
    agent: str
    name: str
    sessions: int
    turns: int
    native_memories: int = 0
    last_activity_at: datetime | None = None


class ActivityBucketSchema(BaseModel):
    date: str
    sessions: int
    turns: int


class StatsResponse(BaseModel):
    total_sessions: int
    total_turns: int
    total_memories: int
    total_native_memories: int
    db_size_bytes: int
    index_embeddings_count: int
    by_agent: list[AgentStatSchema] = Field(default_factory=list)
    memories_by_category: dict[MemoryCategory, int]
    memories_by_status: dict[Literal["heuristic", "llm_distilled", "pending", "manual"], int]
    recent_activity: list[ActivityBucketSchema] = Field(default_factory=list)
    distiller: DistillerSettingsResponse
