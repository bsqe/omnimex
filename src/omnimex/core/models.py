"""Canonical Pydantic v2 domain models for Omnimex."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AgentId = str
ToolCallStatus = Literal["ok", "error", "pending"]
TurnRole = Literal["user", "assistant", "system", "tool"]
MemoryCategory = Literal["preference", "decision", "pitfall", "fact"]
DistillStatus = Literal["pending", "heuristic", "llm_distilled"]
MemoryOrigin = Literal["manual", "distilled", "native_import"]
SearchMode = Literal["hybrid", "fts", "vector"]

_ROLE_ALIASES: dict[str, TurnRole] = {
    "user": "user",
    "human": "user",
    "assistant": "assistant",
    "agent": "assistant",
    "model": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "system": "system",
    "developer": "system",
    "tool": "tool",
    "function": "tool",
}


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_workspace_path(workspace: str | Path | None) -> str | None:
    """Normalize Windows/POSIX workspace slashes for consistent filtering."""
    if workspace is None:
        return None
    text = str(workspace).strip()
    if not text:
        return None
    normalized = text.replace("\\", "/")
    # Preserve drive root like "E:/" or "/" while stripping trailing slashes elsewhere.
    if len(normalized) > 3 or (len(normalized) > 1 and not normalized.endswith(":/")):
        normalized = normalized.rstrip("/")
    return normalized


class ToolCallRecord(BaseModel):
    """A single tool invocation and its outcome inside a conversation turn."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    name: str
    arguments: dict[str, Any] | str = Field(default_factory=dict)
    output: str | None = None
    status: ToolCallStatus = "ok"
    duration_ms: int | None = Field(default=None, ge=0)


class NormalizedTurn(BaseModel):
    """A normalized turn across any supported coding agent."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    session_id: str = ""
    turn_index: int = Field(ge=0)
    role: TurnRole
    content: str = ""
    thinking: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    timestamp: datetime | None = None
    model: str | None = None
    token_count: int | None = Field(default=None, ge=0)
    redacted: bool = False

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> Any:
        if isinstance(value, str):
            mapped = _ROLE_ALIASES.get(value.strip().lower())
            if mapped is not None:
                return mapped
        return value

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _fill_id(self) -> NormalizedTurn:
        if not self.id and self.session_id:
            self.id = f"{self.session_id}#t{self.turn_index}"
        return self


class NormalizedSession(BaseModel):
    """A normalized coding-agent session with optional ordered turns."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    agent: AgentId
    external_id: str
    title: str = ""
    workspace: str | None = None
    source_path: str
    started_at: datetime | None = None
    updated_at: datetime | None = None
    turn_count: int = Field(default=0, ge=0)
    model: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    turns: list[NormalizedTurn] = Field(default_factory=list)
    distill_status: DistillStatus = "pending"

    @field_validator("workspace", mode="before")
    @classmethod
    def _normalize_workspace(cls, value: Any) -> str | None:
        if value is None or isinstance(value, (str, Path)):
            return normalize_workspace_path(value)
        return str(value)

    @field_validator("started_at", "updated_at", mode="after")
    @classmethod
    def _normalize_dates(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _sync_session_fields(self) -> NormalizedSession:
        if not self.id:
            self.id = f"{self.agent}:{self.external_id}"
        if self.turns:
            if self.turn_count == 0:
                self.turn_count = len(self.turns)
            for turn in self.turns:
                if not turn.session_id:
                    turn.session_id = self.id
                if not turn.id:
                    turn.id = f"{self.id}#t{turn.turn_index}"
        return self


class MemoryCard(BaseModel):
    """A central Markdown-backed memory unit with provenance and review state."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex[:12]}")
    title: str
    category: MemoryCategory
    content: str
    file_path: str | None = None
    tags: list[str] = Field(default_factory=list)
    workspace: str | None = None
    distill_status: DistillStatus = "pending"
    origin: MemoryOrigin = "manual"
    reviewed: bool = False
    source_session_id: str | None = None
    source_agent: str | None = None
    source_turn_index: int | None = Field(default=None, ge=0)
    native_source_path: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("workspace", mode="before")
    @classmethod
    def _normalize_workspace(cls, value: Any) -> str | None:
        if value is None or isinstance(value, (str, Path)):
            return normalize_workspace_path(value)
        return str(value)

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _normalize_dates(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized


class SearchHit(BaseModel):
    """A single search hit returned by FTS5, vector, or RRF hybrid search."""

    model_config = ConfigDict(extra="ignore")

    turn_id: str
    session_id: str
    agent: AgentId
    session_title: str = ""
    workspace: str | None = None
    turn_index: int = Field(ge=0)
    role: TurnRole
    snippet: str
    content: str
    timestamp: datetime | None = None
    score: float
    fts_score: float | None = None
    vector_score: float | None = None
    match_mode: SearchMode = "hybrid"

    @field_validator("workspace", mode="before")
    @classmethod
    def _normalize_workspace(cls, value: Any) -> str | None:
        if value is None or isinstance(value, (str, Path)):
            return normalize_workspace_path(value)
        return str(value)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value)


class HandoffMemoryRef(BaseModel):
    """Lightweight memory reference embedded inside a HandoffBrief."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    category: MemoryCategory


class HandoffSections(BaseModel):
    """Structured sections of a cross-agent handoff brief."""

    model_config = ConfigDict(extra="ignore")

    goal: str = ""
    current_state: str = ""
    key_decisions: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    pitfalls_and_constraints: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    relevant_memories: list[HandoffMemoryRef] = Field(default_factory=list)


class HandoffBrief(BaseModel):
    """A redacted, target-agent-tailored handoff brief for resuming work elsewhere."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    source_agent: AgentId
    target_agent: AgentId
    workspace: str | None = None
    title: str
    markdown: str
    sections: HandoffSections = Field(default_factory=HandoffSections)
    token_estimate: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=utc_now)
    redacted: bool = True

    @field_validator("workspace", mode="before")
    @classmethod
    def _normalize_workspace(cls, value: Any) -> str | None:
        if value is None or isinstance(value, (str, Path)):
            return normalize_workspace_path(value)
        return str(value)

    @field_validator("generated_at", mode="after")
    @classmethod
    def _normalize_generated_at(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized


class IngestCursor(BaseModel):
    """Incremental ingestion cursor for tracking per-file byte/line offsets."""

    model_config = ConfigDict(extra="ignore")

    source_path: str
    agent: AgentId
    byte_offset: int = Field(default=0, ge=0)
    line_number: int = Field(default=0, ge=0)
    mtime_ns: int = Field(default=0, ge=0)
    file_size: int = Field(default=0, ge=0)
    checksum: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


SkillInstallTarget = Literal[
    "antigravity",
    "claude_code",
    "codex",
    "cursor",
    "gemini_cli",
    "windsurf",
]


class SkillDraft(BaseModel):
    """Redacted, structured SKILL.md bundle synthesized from sessions and/or memories."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    title: str
    description: str
    skill_md: str
    source_session_ids: list[str] = Field(default_factory=list)
    source_memory_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    workspace: str | None = None
    provider: str = "heuristic"
    redacted: bool = True
    token_estimate: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=utc_now)

    @field_validator("workspace", mode="before")
    @classmethod
    def _normalize_workspace(cls, value: Any) -> str | None:
        if value is None or isinstance(value, (str, Path)):
            return normalize_workspace_path(value)
        return str(value)

    @field_validator("generated_at", mode="after")
    @classmethod
    def _normalize_generated_at(cls, value: datetime) -> datetime:
        normalized = ensure_utc(value)
        assert normalized is not None
        return normalized


class SavedSkillSummary(BaseModel):
    """Metadata and content summary of a curated Skill stored in ``data/skills/``."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    name: str
    description: str = ""
    file_path: str
    updated_at: datetime
    token_estimate: int = Field(default=0, ge=0)
    skill_md: str = ""


class SkillInstallEntry(BaseModel):
    """Installation outcome for one coding-agent skill directory target."""

    target: str
    path: str
    status: Literal["created", "updated", "unchanged"]
    backup_path: str | None = None


class SkillInstallResult(BaseModel):
    """Aggregated outcome from installing a packaged SKILL.md across agent targets."""

    slug: str
    installed: list[SkillInstallEntry] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


__all__ = [
    "AgentId",
    "DistillStatus",
    "HandoffBrief",
    "HandoffMemoryRef",
    "HandoffSections",
    "IngestCursor",
    "MemoryCard",
    "MemoryCategory",
    "MemoryOrigin",
    "NormalizedSession",
    "NormalizedTurn",
    "SavedSkillSummary",
    "SearchHit",
    "SearchMode",
    "SkillDraft",
    "SkillInstallEntry",
    "SkillInstallResult",
    "SkillInstallTarget",
    "ToolCallRecord",
    "ToolCallStatus",
    "TurnRole",
    "ensure_utc",
    "normalize_workspace_path",
    "utc_now",
]
