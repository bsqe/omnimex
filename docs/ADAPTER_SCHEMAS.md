# Omnimex Mainstream Agent Adapter Schemas

This document specifies path discovery, on-disk storage formats, incremental cursor behavior, Windows filesystem/SQLite safety, native memory discovery, and canonical normalization rules (`NormalizedSession`, `NormalizedTurn`, `ToolCallRecord`) for all 11 mainstream coding-agent adapters in `omnimex.adapters`.

## 1. Canonical Normalization Contract

Every adapter emits instances of `omnimex.core.models.NormalizedSession` containing ordered `NormalizedTurn` records and `ToolCallRecord` items, scrubbed through `omnimex.core.redaction.redact_session` before persistence.

| Field | Contract |
|---|---|
| `NormalizedSession.id` | Auto-populated as `f"{agent}:{external_id}"` if omitted |
| `NormalizedSession.agent` | Canonical slug: `antigravity`, `claude_code`, `codex`, `cursor`, `gemini_cli`, `windsurf`, `cline`, `roo_code`, `copilot`, `opencode`, `aider` |
| `NormalizedSession.external_id` | Deterministic provider-native session/composer/task ID (or normalized relative path stem fallback) |
| `NormalizedSession.workspace` | Slash-normalized project directory (`E:/path/to/repo` or `/path/to/repo`) |
| `NormalizedSession.source_path` | Canonical source file path (slash-normalized or OS path string) |
| `NormalizedTurn.role` | `"user" | "assistant" | "system" | "tool"` (aliases `human`→`user`, `agent`/`model`/`ai`/`bot`→`assistant`, `developer`→`system`, `function`→`tool`) |
| `NormalizedTurn.thinking` | Optional reasoning / chain-of-thought text preserved separately from `content` |
| `NormalizedTurn.tool_calls` | List of `ToolCallRecord(id, name, arguments, output, status, duration_ms)` |

---

## 2. Shared Cross-Platform & Windows Safety Rules

1. **Windows `PermissionError` (`WinError 5` / `EACCES`) Resilience:**
   - Scanning `~/.gemini/antigravity/brain/*` or OS profile trees on Windows frequently encounters locked or ACL-restricted sibling directories/files.
   - All directory walks (`os.scandir` / `Path.iterdir`), `stat()` calls, and file opens MUST catch `(PermissionError, OSError)` per entry, log/record diagnostics (`AdapterSourceStatus`), and continue scanning remaining readable sources without aborting.
2. **Read-Only Locked SQLite + WAL Temp-Copy (`state.vscdb`, `opencode.db`, `state_5.sqlite`):**
   - Cursor, Windsurf, VS Code Copilot, OpenCode, and Codex hold active SQLite databases open in WAL mode.
   - Never open live provider `.sqlite` / `.vscdb` / `.db` files in read-write mode, and never mutate `-wal` or `-shm` sidecars.
   - Before querying, copy the primary DB plus sibling `-wal` and `-shm` files (when readable) into a private `tempfile.TemporaryDirectory` via `shutil.copy2`, open the copy with `sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)`, extract rows, and dispose of the temp directory.
3. **Incremental Parsing (`IngestCursor`):**
   - **Append-only JSONL sources** (`antigravity`, `claude_code`, `codex`, `gemini_cli` JSONL, `copilot` JSONL): track `byte_offset`, `line_number`, `mtime_ns`, and `file_size`. When `file_size >= cursor.byte_offset` and `mtime_ns` / `file_size` changed, seek directly to `cursor.byte_offset` and parse only newly appended complete lines (UTF-8 BOM stripped on byte 0; incomplete trailing non-`\n`-terminated JSON lines held back until completed or recovered cleanly). If `file_size < cursor.byte_offset` (truncation/rewrite), reset cursor to `0` and re-parse.
   - **Monolithic JSON, Markdown, and SQLite sources** (`cursor`, `windsurf`, `cline`, `roo_code`, `opencode`, `aider`, legacy `.json`): check `(mtime_ns, file_size, checksum)` against `IngestCursor`; skip when unchanged, re-parse and update `file_size`/`mtime_ns`/`checksum` when modified.
4. **Native Memory Discovery (`discover_native_memories`):**
   - Adapters with native memory stores (`claude_code`, `codex`, `windsurf`) emit `MemoryCard` drafts with `origin="native_import"`, `distill_status="heuristic"`, `reviewed=False`, and `native_source_path` pointing to the source `.md` file (or SQLite row provenance).

---

## 3. Adapter Specifications

### 3.1 Antigravity (`antigravity`)
- **Discovery Roots:**
  - `~/.gemini/antigravity/brain/<session_id>/.system_generated/logs/transcript.jsonl`
  - Sibling full log: `~/.gemini/antigravity/brain/<session_id>/.system_generated/logs/transcript_full.jsonl`
  - Alternate CLI root: `~/.gemini/antigravity-cli/brain/<session_id>/.system_generated/logs/transcript.jsonl`
  - Env override: `ANTIGRAVITY_HOME` (or `OMNIMEX_ANTIGRAVITY_ROOTS`)
- **On-Disk Schema (JSONL):**
  - Each line has: `step_index` (int), `source` (`USER_EXPLICIT` | `MODEL` | `SYSTEM`), `type` (`USER_INPUT` | `PLANNER_RESPONSE` | `VIEW_FILE` | `RUN_COMMAND` | `GENERIC` | `SYSTEM_MESSAGE` | `EPHEMERAL_MESSAGE` | `CONVERSATION_HISTORY`), `status`, `created_at` (ISO-8601), `content`, `thinking`, `tool_calls`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `truncated_fields`.
  - **Truncated-field hydration:** When `transcript.jsonl` contains `truncated_fields` on a step and sibling `transcript_full.jsonl` exists, missing/truncated fields for that `step_index` are hydrated from `transcript_full.jsonl`.
  - **Live vs. CASS `tool_calls` shapes:** Supports both `{"name": str, "args": dict}` (live Windows Antigravity) and `{"name": str, "id": str, "arguments": dict|str}` (reference fixture).
  - **User XML envelope unwrapping:** Extracts `<USER_REQUEST>...</USER_REQUEST>` for user prompt text, extracts `<USER_SETTINGS_CHANGE>` model updates (e.g. `from ... to <Model Name>.`), and strips `<ADDITIONAL_METADATA>` / `<SYSTEM_MESSAGE>` noise wrappers.
  - **Role mapping:** `USER_INPUT` → `user`; `PLANNER_RESPONSE` → `assistant`; `VIEW_FILE` / `RUN_COMMAND` / `MODEL+GENERIC` → `tool`; `SYSTEM_MESSAGE` / `EPHEMERAL_MESSAGE` → `system`; empty `CONVERSATION_HISTORY` → skipped.
- **Native Memories:** `supports_native_memories = False`.

### 3.2 Claude Code (`claude_code`)
- **Discovery Roots:**
  - Respects `CLAUDE_CONFIG_DIR` (`/projects`), else `XDG_CONFIG_HOME/claude-code/projects`, else `~/.claude/projects` and `~/.config/claude/projects`.
  - Session logs: `<projects_root>/<encoded_project>/*.jsonl` (plus nested `*.jsonl` excluding `tool-results/` and `auto-mode-classifier*`, and legacy `.json` / `.claude` files).
  - Workspace index: `~/.claude/history.jsonl` (`{display, pastedContents, timestamp, project, sessionId}`) used to resolve `workspace` by `sessionId` when JSONL records omit `cwd`.
  - Native memories: `<projects_root>/*/memory/*.md` (YAML frontmatter `name`, `description`, `metadata.type`).
- **On-Disk Schema (JSONL):**
  - `type == "user"`: `message.content` string or block list; strips `<system-reminder>`, `<local-command-*>`, `<command-*>` hook tags; block lists of `type == "tool_result"` map to `role = "tool"`.
  - `type == "assistant"`: `message.model`, `message.content` blocks (`text` → content, `thinking` → `thinking`, `tool_use` → `ToolCallRecord`), `message.usage` (`input_tokens` + `output_tokens`).
  - `type == "attachment"`: indexes `queued_command` prompts from `origin.kind in (None, "human")` when `not isMeta` and `commandMode != "task-notification"`.
  - `type == "message"`: generic fallback supporting direct or nested `message` objects.
  - Skips metadata/noise records (`summary`, `file-history-snapshot`, `file-history-delta`, `api-request*`, `atis-latch`, `mode`, `permission-mode`, `tools-used`, `last-prompt`, `progress`, meta `system` rows).
- **Native Memories:** `supports_native_memories = True`. Parses frontmatter from `memory/*.md` into `MemoryCard` drafts (`user`→`preference`, `feedback`→`pitfall`, `project`/`reference`→`fact`).

### 3.3 Codex (`codex`)
- **Discovery Roots:**
  - Respects `CODEX_HOME`, default `~/.codex`.
  - Active & archived rollouts: `<codex_home>/sessions/**/rollout-*.jsonl` (and `*.json`), `<codex_home>/archived_sessions/**/rollout-*.jsonl`.
  - SQLite metadata & memories: `<codex_home>/sqlite/state_5.sqlite` (`threads` table for `title`/`cwd`/`model` enrichment) and `<codex_home>/sqlite/memories_1.sqlite` (`stage1_outputs` table).
  - Native Markdown memories: `<codex_home>/memories/*.md` (`MEMORY.md`, `memory_summary.md`, `raw_memories.md`, `rollout_summaries/*.md`).
- **On-Disk Schema:**
  - Modern JSONL (`{timestamp, type, payload}`):
    - `session_meta`: `payload.id`, `payload.cwd`, `payload.cli_version`, `payload.git.branch`.
    - `turn_context`: `payload.cwd`, `payload.model`.
    - `response_item`: `message` (`role`, `content` list of `input_text`/`output_text`/`text`), `reasoning` (`summary[].text` / `text`), `function_call` (`name`, `arguments`, `call_id`), `custom_tool_call` (`name`, `input`, `call_id`), `function_call_output` (`call_id`, `output`).
    - `event_msg`: `user_message`, `agent_message`, `agent_reasoning`, `tool_call`, `tool_result`, `token_count` (`input_tokens + output_tokens`).
    - **Leading host envelope suppression:** Leading `# AGENTS.md instructions...<INSTRUCTIONS>...`, `<environment_context>...</environment_context>`, and `<user_instructions>...</user_instructions>` immediately following a `developer` policy message and preceding the first real user prompt are filtered out; literal occurrences later in conversation are preserved.
    - **Cross-schema deduplication:** Adjacent `event_msg` and `response_item` duplicates sharing the same `id` and text collapse once.
  - Legacy JSON (`rollout-*.json`): `{"session": {"id", "cwd"}, "items": [...]}`.
- **Native Memories:** `supports_native_memories = True`. Parses `<codex_home>/memories/*.md` and `memories_1.sqlite` (`stage1_outputs`) via safe temp copy.

### 3.4 Cursor (`cursor`)
- **Discovery Roots (Windows / POSIX):**
  - Windows: `%APPDATA%/Cursor/User/globalStorage/state.vscdb`, `%APPDATA%/Cursor/User/workspaceStorage/*/state.vscdb` (with sibling `workspace.json`).
  - POSIX / macOS: `~/.config/Cursor/User/...`, `~/Library/Application Support/Cursor/User/...`, `~/.cursor/`.
- **On-Disk Schema (`state.vscdb` SQLite, safe read-only temp copy):**
  - `cursorDiskKV` table:
    - `composerData:<id>`: supports inline `tabs[].bubbles[]` (v0.3x/0.40), `fullConversationHeadersOnly[].bubbleId` resolved against `bubbleId:<composer_id>:<bubble_id>` rows (v0.40+ lazy format), and simple `text`/`richText` composers.
    - Bubble `type`: `1` / `"user"` → `user`; `2` / `"ai"` → `assistant`; `99` / other → `assistant` (tool/system text fallback). Content keys: `text`, `rawText`, `content`, `message`.
    - Workspace extraction from bubble `workspaceProjectDir`, `workspaceUris` (`file://...` or `vscode-remote://...`), or sibling `workspace.json`.
  - `ItemTable` table:
    - Legacy `workbench.panel.aichat.view.aichat.chatdata` (`tabs[].bubbles[]`).
- **Also supports** normalized Cursor JSONL transcripts if encountered under explicit `.cursor` roots.
- **Native Memories:** `supports_native_memories = False`.

### 3.5 Gemini CLI (`gemini_cli`)
- **Discovery Roots:**
  - Respects `GEMINI_HOME`, default `~/.gemini/tmp/<project_hash>/chats/session-*.json` and `session-*.jsonl` (explicitly ignores `~/.gemini/antigravity*`).
- **On-Disk Schema:**
  - Legacy JSON (`session-*.json`): `{sessionId, projectHash, startTime, lastUpdated, messages: [{type: "user"|"model"|"gemini", content, timestamp}]}`.
  - Event-sourced JSONL (`session-*.jsonl`): metadata header (`kind: "main", sessionId, projectHash, startTime`), `$set` patch records (`$set.messages`, `$set.lastUpdated`), and appended message lines (`{id, type, timestamp, content, toolCalls}`).
  - Maps `model` / `gemini` → `assistant`.
  - Extracts workspace from `# AGENTS.md instructions for <path>` or `Working directory: <path>`, else falls back to parent hash directory.
- **Native Memories:** `supports_native_memories = False`.

### 3.6 Windsurf (`windsurf`)
- **Discovery Roots (Windows / POSIX):**
  - Windows: `%APPDATA%/Windsurf/User/globalStorage/state.vscdb`, `%APPDATA%/Windsurf/User/workspaceStorage/*/state.vscdb`
  - POSIX / macOS: `~/.config/Windsurf/User/...`, `~/Library/Application Support/Windsurf/User/...`
  - Codeium/Windsurf roots: `~/.codeium/windsurf/memories/*.md`, `~/.codeium/windsurf/cascade/*.json(l)`, `~/.windsurf/`
- **On-Disk Schema:**
  - SQLite (`state.vscdb`, safe temp copy): inspects `ItemTable` and `cursorDiskKV` for `workbench.panel.aichat.view.aichat.chatdata`, `composerData:*`, `codeium.cascade*`, `windsurf.chat*`, `interactive.sessions`.
  - JSON/JSONL chat logs (`messages` / `turns` / `steps`). Binary `.pb` files are safely ignored.
- **Native Memories:** `supports_native_memories = True` (`~/.codeium/windsurf/memories/*.md`).

### 3.7 Cline & Roo Code (`cline`, `roo_code` in `cline_roo.py`)
- **Discovery Roots (Windows / POSIX):**
  - Cline (`saoudrizwan.claude-dev`): `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/<task_id>/` (plus `Code - Insiders`, `VSCodium`, `Cursor`, `~/.config/Code/...`).
  - Roo Code (`rooveterinaryinc.roo-cline`): `%APPDATA%/Code/User/globalStorage/rooveterinaryinc.roo-cline/tasks/<task_id>/` (plus same IDE variants).
- **On-Disk Schema (multi-file task directory):**
  - `ui_messages.json` (preferred) or `api_conversation_history.json` (fallback when `ui_messages.json` missing/empty).
  - `task_metadata.json`: optional `title`, `rootPath`, `cwd`, `workspace`.
  - Skips non-task `taskHistory.json`.
  - Message normalization: `role` or `type` (`ask`/`user`→`user`, `say`/`assistant`→`assistant`), content from `content` / `text` / `message` (string or Anthropic content blocks), timestamps in epoch seconds (`< 1e11`), epoch milliseconds, or ISO-8601.
- **Native Memories:** `supports_native_memories = False`.

### 3.8 GitHub Copilot (`copilot`)
- **Discovery Roots (Windows / POSIX):**
  - VS Code native chat stores: `%APPDATA%/Code/User/workspaceStorage/*/chatSessions/*.json` and `*.jsonl` (also `Code - Insiders`, `VSCodium`, `~/.config/Code/...`).
  - VS Code legacy state DBs: `workspaceStorage/*/state.vscdb` and `globalStorage/state.vscdb` (`ItemTable` key `interactive.sessions`, filtered to Copilot `responderUsername` or unset to avoid misattributing other chat extensions).
  - Copilot Chat extension export: `globalStorage/github.copilot-chat/*.json`.
  - Copilot CLI: `~/.copilot/session-state/<uuid>/events.jsonl` and `~/.copilot/history-session-state/*.json`.
- **On-Disk Schema:**
  - VS Code JSON (`requests[].message.text`, `requests[].response[]` with `value` and `toolInvocationSerialized` tool calls) and append-log JSONL (`{"kind": 0, "v": {...}}`).
  - Extension export JSON (`turns[]` or `messages[]` or `{conversations: [...]}`).
  - Copilot CLI Chronicle `events.jsonl` (`session.start`, `user.message`, `assistant.message` with nested `data.content`, `data.toolRequests`, or flat `prompt`/`output`/`result`) and legacy `history[]` JSON (`role: "human"|"assistant"`).
- **Native Memories:** `supports_native_memories = False`.

### 3.9 OpenCode & Aider (`opencode`, `aider` in `opencode_aider.py`)
- **OpenCode (`opencode`):**
  - **Discovery Roots:**
    - SQLite: `%LOCALAPPDATA%/opencode/opencode.db`, `~/.local/share/opencode/opencode.db` (safe temp copy).
    - JSON tree: `~/.local/share/opencode/storage/` (`session/<proj>/*.json`, `message/<ses_id>/*.json`, `part/<msg_id>/*.json`).
    - JSONL session exports if present.
  - **Schema:**
    - SQLite 2.x (`session_v2` + `session_message` with `user`, `assistant`, `idle`, `location-switched` boundary filtering) taking precedence over SQLite 1.x Drizzle (`session`, `message`, `part`) for migrated session IDs.
    - JSON tree joins `session` → `message` → `part` (`text`, `tool`, `reasoning`) sorted by creation timestamp.
- **Aider (`aider`):**
  - **Discovery Roots:** `.aider.chat.history.md` under configured workspace/scan roots or `~`.
  - **Schema:** Markdown dialogue log where `# aider chat started at ...` sets timestamp context, consecutive `> ` lines form a `user` turn (stripping `> `), and non-`>` prose/code between prompts forms `assistant` turns (`system` if preceding any user prompt).
- **Native Memories:** `supports_native_memories = False` for both.
