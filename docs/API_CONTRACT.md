# Omnimex API Contract (REST + SSE)

**Version:** `1.0.0`  
**Default Base URL:** `http://127.0.0.1:8765` (Frontend dev server proxies `/api/*` to this origin)  
**Media Type:** `application/json; charset=utf-8` (REST), `text/event-stream; charset=utf-8` (SSE)

---

## 1. Conventions & Security

- **Timestamps:** ISO-8601 UTC strings (e.g. `2026-11-17T07:56:23Z`).
- **Workspace Path Normalization:** Windows backslashes (`E:\foo\bar`) and forward slashes (`E:/foo/bar`) are treated equivalently when filtering by `workspace`. Responses preserve canonical workspace strings.
- **Secret Redaction Guarantee:** All transcript turns, tool call arguments/outputs, memory cards, and generated handoff briefs are scrubbed through `omnimex.core.redaction` before persistence and API serialization. API keys in `PATCH /api/settings/distiller` are write-only and are never returned in `GET` responses.
- **Standard Error Envelope:**

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Session 'abc' was not found",
    "details": {}
  }
}
```

Standard `error.code` values: `VALIDATION_ERROR` (`400`/`422`), `NOT_FOUND` (`404`), `CONFLICT` (`409`), `PERMISSION_DENIED` (`403`), `ADAPTER_ERROR` (`500`), `DISTILLER_ERROR` (`502`), `INTERNAL_ERROR` (`500`).

---

## 2. Canonical Domain Types

### `AgentId`
`"antigravity" | "claude_code" | "codex" | "cursor" | "gemini_cli" | "windsurf" | "cline" | "roo_code" | "copilot" | "opencode" | "aider"` (or custom adapter slug).

### `ToolCallRecord`
```json
{
  "id": "tc_01",
  "name": "run_command",
  "arguments": { "CommandLine": "pytest -q" },
  "output": "14 passed in 1.12s",
  "status": "ok",
  "duration_ms": 1120
}
```
- `status`: `"ok" | "error" | "pending"`
- `arguments`: JSON object or raw string

### `NormalizedTurn`
```json
{
  "id": "turn_01",
  "session_id": "antigravity:3521e894",
  "turn_index": 0,
  "role": "assistant",
  "content": "Implemented FTS5 hybrid search with RRF scoring.",
  "thinking": "Checked SQLite WAL locking semantics first...",
  "tool_calls": [],
  "timestamp": "2026-11-17T07:56:23Z",
  "model": "claude-sonnet-4-5",
  "token_count": 412,
  "redacted": false
}
```
- `role`: `"user" | "assistant" | "system" | "tool"`

### `NormalizedSession`
```json
{
  "id": "antigravity:3521e894",
  "agent": "antigravity",
  "external_id": "3521e894-2ac0-4415-b743-f63729c97870",
  "title": "Implement SQLite FTS5 hybrid search",
  "workspace": "E:/AAAAA project test/omnimex",
  "source_path": "~/.gemini/antigravity/brain/3521e894/.system_generated/logs/transcript.jsonl",
  "started_at": "2026-11-17T07:40:00Z",
  "updated_at": "2026-11-17T07:56:23Z",
  "turn_count": 18,
  "model": "claude-sonnet-4-5",
  "tags": ["sqlite", "search"],
  "summary": "Added FTS5 virtual table and RRF vector fusion.",
  "distill_status": "pending",
  "turns": []
}
```
- `turns` is omitted or `[]` in list responses (`GET /api/sessions`) and populated in detail responses (`GET /api/sessions/{id}`).
- `distill_status`: `"pending" | "heuristic" | "llm_distilled"`

### `MemoryCard`
```json
{
  "id": "mem_sqlite_wal",
  "title": "Always enable SQLite WAL and temp-copy IDE state DBs on Windows",
  "category": "pitfall",
  "content": "## Context\nCursor and Windsurf hold write locks on `state.vscdb`...",
  "file_path": "pitfalls/sqlite-wal-windows.md",
  "tags": ["windows", "sqlite", "cursor"],
  "workspace": "E:/AAAAA project test/omnimex",
  "distill_status": "heuristic",
  "origin": "distilled",
  "reviewed": false,
  "source_session_id": "codex:rollout-2026-11-16",
  "source_agent": "codex",
  "source_turn_index": 11,
  "native_source_path": null,
  "confidence": 0.86,
  "created_at": "2026-11-17T06:10:00Z",
  "updated_at": "2026-11-17T06:10:00Z"
}
```
- `category`: `"preference" | "decision" | "pitfall" | "fact"`
- `distill_status`: `"pending" | "heuristic" | "llm_distilled"`
- `origin`: `"manual" | "distilled" | "native_import"`
- `file_path`: `string | null` (relative to `data/memories`, or `null` before disk persistence)
- `reviewed`: `boolean` (`false` for draft memories awaiting user review; `true` for verified/manual memories)

### `SearchHit`
```json
{
  "turn_id": "turn_14",
  "session_id": "claude_code:proj-omnimex-01",
  "agent": "claude_code",
  "session_title": "Design adapter discovery and cursor state",
  "workspace": "E:/AAAAA project test/omnimex",
  "turn_index": 14,
  "role": "assistant",
  "snippet": "Use a byte-offset cursor in ingest_cursors so incremental scans skip...",
  "content": "Full turn content...",
  "timestamp": "2026-11-17T05:12:00Z",
  "score": 0.914,
  "fts_score": 0.87,
  "vector_score": 0.89,
  "match_mode": "hybrid"
}
```

### `HandoffBrief`
```json
{
  "session_id": "antigravity:3521e894",
  "source_agent": "antigravity",
  "target_agent": "claude_code",
  "workspace": "E:/AAAAA project test/omnimex",
  "title": "Handoff: Implement SQLite FTS5 hybrid search",
  "markdown": "# Cross-Agent Handoff Brief\n...",
  "sections": {
    "goal": "Implement RRF hybrid search over SQLite FTS5 and local ONNX embeddings.",
    "current_state": "Core storage schema and embeddings module passing pytest.",
    "key_decisions": ["Use Reciprocal Rank Fusion (k=60) over FTS5 BM25 + float32 cosine similarity."],
    "modified_files": ["src/omnimex/storage/db.py", "src/omnimex/storage/search.py"],
    "pitfalls_and_constraints": ["Do not download ONNX models during offline test runs."],
    "next_steps": ["Wire FastAPI /api/search endpoint and verify SSE event dispatch."],
    "relevant_memories": [
      { "id": "mem_sqlite_wal", "title": "Always enable SQLite WAL...", "category": "pitfall" }
    ]
  },
  "token_estimate": 385,
  "generated_at": "2026-11-17T08:05:00Z",
  "redacted": true
}
```

---

## 3. REST Endpoints

### 3.1 Health

#### `GET /api/health`
Lightweight readiness probe.
- **Response `200 OK`:**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "db_ready": true,
  "distiller_provider": "heuristic"
}
```

---

### 3.2 Hybrid Search

#### `GET /api/search`
Search across normalized conversation turns using FTS5 BM25, local ONNX vector similarity, or Reciprocal Rank Fusion (`hybrid`).

- **Query Parameters:**
  - `q` (`string`, optional): Search query. Empty query returns recent matching turns when facets are provided.
  - `agent` (`string`, optional): Comma-separated agent slugs (e.g. `antigravity,claude_code`).
  - `workspace` (`string`, optional): Workspace path filter (prefix or exact match, slash-normalized).
  - `mode` (`"hybrid" | "fts" | "vector"`, default `"hybrid"`): Search retrieval strategy.
  - `date_from` (`string`, optional): ISO-8601 date/timestamp lower bound (inclusive).
  - `date_to` (`string`, optional): ISO-8601 date/timestamp upper bound (inclusive).
  - `limit` (`integer`, default `20`, max `100`).
  - `offset` (`integer`, default `0`).

- **Response `200 OK`:**
```json
{
  "query": "sqlite wal",
  "mode": "hybrid",
  "total": 4,
  "took_ms": 11.8,
  "hits": [],
  "facets": {
    "agents": { "antigravity": 2, "codex": 2 },
    "workspaces": { "E:/AAAAA project test/omnimex": 4 }
  }
}
```

---

### 3.3 Sessions List & Detail

#### `GET /api/sessions`
List ingested sessions sorted by `updated_at` descending.

- **Query Parameters:**
  - `agent` (`string`, optional): Comma-separated agent filter.
  - `workspace` (`string`, optional): Workspace filter.
  - `q` (`string`, optional): Title / summary / tag substring filter.
  - `date_from` (`string`, optional): ISO-8601 lower bound on `updated_at`.
  - `date_to` (`string`, optional): ISO-8601 upper bound on `updated_at`.
  - `limit` (`integer`, default `50`, max `200`).
  - `offset` (`integer`, default `0`).

- **Response `200 OK`:**
```json
{
  "total": 42,
  "limit": 50,
  "offset": 0,
  "sessions": []
}
```

#### `GET /api/sessions/{id}`
Fetch paginated session metadata and turn-by-turn conversation (including `thinking` blocks and `tool_calls`), defaulting to the latest 40 turns with oversized field truncation for smooth UI rendering.

- **Path Parameters:**
  - `id` (`string`): URL-encoded `NormalizedSession.id`.
- **Query Parameters:**
  - `turn_offset` (`integer`, optional): Zero-based starting turn offset (omitting returns the latest `turn_limit` turns).
  - `turn_limit` (`integer`, default `40`, `1..200`): Maximum turns returned per page.
  - `max_turn_chars` (`integer`, default `12000`, `0..500000`): Maximum characters per field before truncation (`0` disables truncation).
- **Response `200 OK`:**
```json
{
  "session": { "...NormalizedSession with turns[]..." },
  "linked_memories": [],
  "total_turns": 120,
  "turn_offset": 80,
  "turn_limit": 40,
  "has_earlier_turns": true,
  "has_later_turns": false,
  "truncated_turn_indices": []
}
```
- **Errors:** `404 NOT_FOUND` if session ID does not exist.

#### `GET /api/sessions/{id}/turns/{turn_index}`
Fetch a single full, untruncated turn by `turn_index` (used when expanding oversized turns in the UI).

- **Response `200 OK`:**
```json
{
  "turn": { "...full NormalizedTurn..." },
  "truncated": false
}
```

---

### 3.4 Central Markdown Memories CRUD, Review & Sync

Memories are stored as human-readable Markdown files with YAML frontmatter inside the configured central memory directory (`data/memories/`), indexed into SQLite for search and provenance tracking.

#### `GET /api/memories`
List central memories with optional filtering and native memory import status.

- **Query Parameters:**
  - `category` (`"preference" | "decision" | "pitfall" | "fact"`, optional)
  - `distill_status` (`"pending" | "heuristic" | "llm_distilled"`, optional)
  - `origin` (`"manual" | "distilled" | "native_import"`, optional)
  - `reviewed` (`boolean`, optional)
  - `workspace` (`string`, optional)
  - `agent` (`string`, optional): Filter by `source_agent`.
  - `q` (`string`, optional): Title/content/tag filter.

- **Response `200 OK`:**
```json
{
  "total": 12,
  "counts_by_category": {
    "preference": 3,
    "decision": 4,
    "pitfall": 3,
    "fact": 2
  },
  "counts_by_status": {
    "heuristic": 5,
    "llm_distilled": 2,
    "pending": 1,
    "manual": 4
  },
  "native_imports": [
    {
      "agent": "claude_code",
      "source_dir": "~/.claude/projects/*/memory/",
      "discovered_files": 3,
      "imported_count": 3,
      "last_imported_at": "2026-11-17T07:50:00Z",
      "status": "synced"
    },
    {
      "agent": "codex",
      "source_dir": "~/.codex/memories/",
      "discovered_files": 2,
      "imported_count": 2,
      "last_imported_at": "2026-11-17T07:50:00Z",
      "status": "synced"
    }
  ],
  "memories": []
}
```

#### `POST /api/memories`
Create a new central Markdown memory card (writes `.md` file + indexes in SQLite).

- **Request Body:**
```json
{
  "title": "Prefer npm in frontend workspace on Windows",
  "category": "preference",
  "content": "pnpm is not installed in this environment; always use `npm` scripts.",
  "tags": ["frontend", "windows", "tooling"],
  "workspace": "E:/AAAAA project test/omnimex",
  "source_session_id": null,
  "source_agent": null,
  "reviewed": true
}
```
- **Response `201 Created`:** Returns the created `MemoryCard`.

#### `PATCH /api/memories/{id}`
Update a memory card's metadata, Markdown body, category, or draft review state (`reviewed: true`). Updates both the underlying `.md` file and SQLite index.

- **Request Body (all fields optional):**
```json
{
  "title": "Updated title",
  "category": "decision",
  "content": "Updated markdown content...",
  "tags": ["sqlite"],
  "workspace": "E:/AAAAA project test/omnimex",
  "reviewed": true,
  "distill_status": "llm_distilled"
}
```
- **Response `200 OK`:** Returns updated `MemoryCard`.

#### `DELETE /api/memories/{id}`
Remove a memory card from SQLite and delete its backing Markdown file.

- **Response `200 OK`:**
```json
{
  "deleted": true,
  "id": "mem_sqlite_wal"
}
```

#### `POST /api/memories/sync`
Reconcile central Markdown files (`data/memories/**/*.md`) with SQLite and optionally import native agent memories (Claude Code `MEMORY.md`, Codex `memories/*.md`, etc.).

- **Request Body (optional):**
```json
{
  "import_native": true,
  "overwrite_existing": false
}
```
- **Response `200 OK`:**
```json
{
  "synced_files": 12,
  "added": 2,
  "updated": 1,
  "deleted": 0,
  "imported_native": 5,
  "native_imports": []
}
```

---

### 3.5 Cross-Agent Handoff Generator

#### `POST /api/handoff`
Generate a redacted, target-agent-tailored `HandoffBrief` from an existing session and its relevant central memories.

- **Request Body:**
```json
{
  "session_id": "antigravity:3521e894",
  "target_agent": "claude_code",
  "include_memories": true,
  "include_tool_summary": true,
  "max_tokens": 1200,
  "custom_instructions": "Focus on remaining API wiring tasks."
}
```
- **Response `200 OK`:** Returns `HandoffBrief`.
- **Errors:** `404 NOT_FOUND` if `session_id` does not exist.

---

### 3.6 Adapters Health, Scan Trigger & Analytics

#### `GET /api/adapters`
Report discovery health, Windows permission diagnostics, watcher status, and counts for every registered adapter.

- **Response `200 OK`:**
```json
{
  "total_discovered_sources": 7,
  "watcher_running": true,
  "adapters": [
    {
      "id": "antigravity",
      "name": "Antigravity",
      "status": "healthy",
      "source_paths": ["~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl"],
      "discovered_files": 14,
      "session_count": 14,
      "turn_count": 318,
      "native_memory_count": 0,
      "last_scan_at": "2026-11-17T07:55:00Z",
      "last_error": null,
      "watcher_active": true,
      "supports_native_memories": false
    }
  ]
}
```
- `status`: `"healthy" | "degraded" | "not_found" | "permission_denied"`

#### `POST /api/scan`
Trigger incremental (or full) ingestion across all or selected adapters. Supports JSON response by default, or SSE stream when `Accept: text/event-stream` or `?stream=true` is passed.

- **Request Body (optional):**
```json
{
  "agents": ["antigravity", "claude_code", "codex"],
  "full_rescan": false,
  "distill": true
}
```
- **Response `200 OK` (JSON):**
```json
{
  "job_id": "scan_20261117_080000",
  "status": "completed",
  "scanned_files": 34,
  "new_sessions": 3,
  "new_turns": 64,
  "updated_sessions": 2,
  "errors": [],
  "duration_ms": 245.6
}
```

#### `GET /api/stats`
Aggregated workspace, session, turn, memory, and distiller telemetry for the dashboard.

- **Response `200 OK`:**
```json
{
  "total_sessions": 42,
  "total_turns": 984,
  "total_memories": 18,
  "total_native_memories": 5,
  "db_size_bytes": 2457600,
  "index_embeddings_count": 984,
  "by_agent": [
    {
      "agent": "antigravity",
      "name": "Antigravity",
      "sessions": 14,
      "turns": 318,
      "native_memories": 0,
      "last_activity_at": "2026-11-17T07:55:00Z"
    }
  ],
  "memories_by_category": {
    "preference": 4,
    "decision": 6,
    "pitfall": 5,
    "fact": 3
  },
  "memories_by_status": {
    "heuristic": 9,
    "llm_distilled": 3,
    "pending": 2,
    "manual": 4
  },
  "recent_activity": [
    { "date": "2026-11-17", "sessions": 6, "turns": 142 }
  ],
  "distiller": {
    "provider": "heuristic",
    "model": null,
    "base_url": null,
    "api_key_configured": false,
    "auto_distill_on_scan": true,
    "zero_cost_mode": true,
    "pending_sessions_count": 2,
    "distilled_memories_count": 12
  }
}
```

---

### 3.7 Distiller Settings & Backfill Trigger

#### `GET /api/settings/distiller`
Read current distiller configuration (`heuristic` zero-cost default vs `ollama` vs `openai_compatible`) and pending backfill counts.

- **Response `200 OK`:**
```json
{
  "provider": "heuristic",
  "model": null,
  "base_url": null,
  "api_key_configured": false,
  "auto_distill_on_scan": true,
  "zero_cost_mode": true,
  "pending_sessions_count": 2,
  "distilled_memories_count": 12
}
```

#### `PATCH /api/settings/distiller`
Switch distiller provider or configure local Ollama / OpenAI-compatible endpoint.

- **Request Body (partial updates allowed):**
```json
{
  "provider": "ollama",
  "model": "qwen2.5:7b-instruct",
  "base_url": "http://127.0.0.1:11434/v1",
  "api_key": null,
  "auto_distill_on_scan": true
}
```
- **Response `200 OK`:** Returns sanitized `DistillerSettings` (`api_key` omitted; `api_key_configured: boolean`).

#### `POST /api/distill/backfill`
Run memory distillation over pending/un-distilled sessions using the currently active distiller provider. Supports JSON response or SSE (`Accept: text/event-stream` / `?stream=true`).

- **Request Body (optional):**
```json
{
  "limit": 25,
  "agent": null,
  "force": false
}
```
- **Response `200 OK`:**
```json
{
  "job_id": "distill_20261117_080200",
  "status": "completed",
  "provider": "heuristic",
  "processed_sessions": 2,
  "created_memories": 4,
  "skipped_sessions": 0
}
```

---

## 4. Server-Sent Events (SSE)

#### `GET /api/events`
Unified real-time event stream for live file watcher ingestions, adapter scans, memory syncs, and distillation backfill progress. Clients connect via standard `EventSource("/api/events")`.

- **Optional Query Parameters:**
  - `topics` (`string`, optional): Comma-separated filter (`scan,session,memory,distill`).

- **Event Stream Format:**
```text
event: connected
data: {"server_time":"2026-11-17T08:00:00Z","watcher_running":true}

event: scan.started
data: {"job_id":"scan_20261117_080000","agents":["antigravity","claude_code","codex"]}

event: scan.progress
data: {"job_id":"scan_20261117_080000","agent":"claude_code","scanned_files":8,"total_files":14,"new_sessions":1,"new_turns":24}

event: session.ingested
data: {"session_id":"claude_code:proj-01","agent":"claude_code","title":"Wire FastAPI endpoints","new_turns":12}

event: scan.completed
data: {"job_id":"scan_20261117_080000","new_sessions":3,"new_turns":64,"errors_count":0,"duration_ms":245.6}

event: memory.synced
data: {"synced_files":12,"imported_native":5}

event: distill.progress
data: {"job_id":"distill_20261117_080200","processed_sessions":1,"total_sessions":2,"created_memories":2,"provider":"heuristic"}

event: distill.completed
data: {"job_id":"distill_20261117_080200","processed_sessions":2,"created_memories":4,"provider":"heuristic"}

event: heartbeat
data: {"ts":"2026-11-17T08:00:15Z"}
```

When `POST /api/scan?stream=true` or `POST /api/distill/backfill?stream=true` is invoked with `Accept: text/event-stream`, the response streams the corresponding `*.started`, `*.progress`, and `*.completed` (or `error`) SSE events directly on that response before closing.
