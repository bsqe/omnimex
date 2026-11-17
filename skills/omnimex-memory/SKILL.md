---
name: omnimex-memory
description: Progressive cross-agent memory recall, session history search, central Markdown memory capture, and cross-agent handoffs via the Omnimex MCP server. Use before starting non-trivial tasks, after solving tricky bugs or making architectural decisions, and when handing off between coding agents.
---

# Omnimex Cross-Agent Memory Workflow

You have access to the **Omnimex** MCP server, a shared cross-agent memory and session search hub backed by central Markdown files (`data/memories/`) and local SQLite FTS5 + vector RRF search.

## Core Principles

1. **Progressive Disclosure (Low Token Cost):** Never dump huge raw transcripts. Always search/recall compact snippets first, then read only the exact session turn window or memory card you need.
2. **Central Markdown Truth:** All durable memories created by agents are written **only** to Omnimex's central `data/memories/` store via `memory_save` / `memory_update`. Never modify native `~/.claude/` or `~/.codex/` memory files.
3. **Zero Secret Leakage:** Omnimex scrubs API keys, tokens, JWTs, credentialed URLs, and private keys automatically, and you must also avoid pasting raw secrets into memory cards.

## 1. Before Starting Non-Trivial Work

Check whether another coding agent (Antigravity, Claude Code, Codex, Cursor, Windsurf, Gemini CLI, etc.) already solved part of this problem or recorded workspace rules:

1. Call `memory_recall(query="<topic>", workspace="<workspace path>", limit=8)` to fetch compact memory summaries (`id`, `title`, `category`, `preview`).
2. Call `history_search(query="<topic>", workspace="<workspace path>", mode="hybrid", limit=8)` to locate relevant past turns across agents.
3. Expand only relevant matches:
   - `memory_read(memory_id="<id>")` for the full Markdown card.
   - `history_read(session_id="<session_id>", start_turn=<n>, limit=20)` for context around a matching turn.

## 2. Capturing Durable Knowledge (Option 3 Workflow)

When you resolve a non-trivial bug, discover a Windows/environment pitfall, confirm a user tooling preference, or make an architectural decision, persist it immediately:

- Call `memory_save`:
  - `title`: Short imperative summary (e.g., `"Use npm instead of pnpm on Windows"`)
  - `category`: `"preference"` | `"decision"` | `"pitfall"` | `"fact"`
  - `content`: Concise Markdown explanation including exact file paths, error messages, and verification commands
  - `workspace`: Workspace path (forward slashes preferred)
  - `tags`: Short lowercase keywords
  - `source_session_id` / `source_agent`: Include when known for provenance
- If `memory_save` returns `conflicts`, inspect the conflicting card with `memory_read` and update or reconcile it via `memory_update`.

## 3. Cross-Agent Handoff

When handing a task off to another agent (e.g., Antigravity -> Claude Code or Codex -> Cursor):

1. Call `session_handoff(session_id="<id>", target_agent="<target>", include_memories=True, custom_instructions="...")`.
2. Share the returned redacted Markdown brief so the next agent starts with goal, current state, modified files, decisions, pitfalls, and next steps.
