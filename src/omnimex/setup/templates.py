"""MCP client configuration templates and AGENTS.md/CLAUDE.md rule snippets."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

OMNIMEX_RULES_START = "<!-- OMNIMEX:START -->"
OMNIMEX_RULES_END = "<!-- OMNIMEX:END -->"

OMNIMEX_AGENT_RULES_SNIPPET = f"""{OMNIMEX_RULES_START}
## Omnimex Cross-Agent Memory Workflow

Use the `omnimex` MCP server with **progressive disclosure** (low token cost):
1. **Before non-trivial tasks:** Call `memory_recall(query=..., workspace=...)` and `history_search(query=..., workspace=...)` to surface relevant decisions, pitfalls, and prior agent sessions.
2. **Expand only what you need:** Use `memory_read(memory_id)` or `history_read(session_id, start_turn=..., limit=20)` instead of dumping full logs.
3. **Persist durable learnings:** When you solve a tricky bug, confirm a user preference, or make an architectural decision, call `memory_save(title=..., category="preference|decision|pitfall|fact", content=..., workspace=...)`.
4. **Cross-agent handoff:** Call `session_handoff(session_id, target_agent=...)` when switching tools.
5. **Central Markdown Truth:** Always save memories via `memory_save`; never overwrite native `~/.claude` or `~/.codex` memory files.
{OMNIMEX_RULES_END}
"""


def resolve_omnimex_command(explicit_command: str | None = None) -> tuple[str, list[str]]:
    """Resolve the safest executable + args to launch ``omnimex mcp``."""
    if explicit_command:
        return explicit_command, ["mcp"]

    venv_exe = Path(sys.executable).parent / (
        "omnimex.exe" if sys.platform == "win32" else "omnimex"
    )
    if venv_exe.is_file():
        return str(venv_exe), ["mcp"]

    on_path = shutil.which("omnimex")
    if on_path:
        return on_path, ["mcp"]

    return sys.executable, ["-m", "omnimex.cli", "mcp"]


def build_json_mcp_entry(
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return standard JSON ``mcpServers.omnimex`` object."""
    entry: dict[str, Any] = {
        "command": command,
        "args": list(args),
    }
    if env:
        entry["env"] = dict(env)
    return entry


def build_codex_toml_block(
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Return TOML ``[mcp_servers.omnimex]`` configuration block for Codex."""
    escaped_cmd = command.replace("\\", "\\\\").replace('"', '\\"')
    escaped_args = ", ".join(
        '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' for a in args
    )
    lines = [
        "[mcp_servers.omnimex]",
        f'command = "{escaped_cmd}"',
        f"args = [{escaped_args}]",
    ]
    if env:
        lines.append("")
        lines.append("[mcp_servers.omnimex.env]")
        for k, v in env.items():
            escaped_val = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped_val}"')
    return "\n".join(lines) + "\n"


__all__ = [
    "OMNIMEX_AGENT_RULES_SNIPPET",
    "OMNIMEX_RULES_END",
    "OMNIMEX_RULES_START",
    "build_codex_toml_block",
    "build_json_mcp_entry",
    "resolve_omnimex_command",
]
