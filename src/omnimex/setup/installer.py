"""Safe MCP client setup generator/injector (--dry-run default, .bak backups on --apply)."""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omnimex.setup.templates import (
    OMNIMEX_AGENT_RULES_SNIPPET,
    OMNIMEX_RULES_END,
    OMNIMEX_RULES_START,
    build_codex_toml_block,
    build_json_mcp_entry,
    resolve_omnimex_command,
)

ClientName = Literal[
    "antigravity",
    "claude_code",
    "codex",
    "cursor",
    "windsurf",
    "gemini_cli",
]

SUPPORTED_MCP_CLIENTS: tuple[ClientName, ...] = (
    "antigravity",
    "claude_code",
    "codex",
    "cursor",
    "windsurf",
    "gemini_cli",
)

_CODEX_SECTION_RE = re.compile(
    r"(?ms)^\[mcp_servers\.omnimex\]\r?\n(?:^(?!\[)[^\r\n]*\r?\n?)*"
    r"(?:\r?\n*^\[mcp_servers\.omnimex\.[^\]]+\]\r?\n(?:^(?!\[)[^\r\n]*\r?\n?)*)*"
)
_RULES_BLOCK_RE = re.compile(
    re.escape(OMNIMEX_RULES_START) + r"[\s\S]*?" + re.escape(OMNIMEX_RULES_END) + r"\r?\n?"
)


@dataclass(slots=True)
class SetupActionResult:
    """Result or dry-run preview of configuring one MCP client or rules file."""

    client: str
    target_path: Path
    format: Literal["json", "toml", "markdown"]
    would_create: bool
    would_backup: bool
    backup_path: Path | None
    preview_content: str
    applied: bool = False


@dataclass(slots=True)
class SetupReport:
    """Aggregated dry-run or applied setup report."""

    apply: bool
    actions: list[SetupActionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def default_client_config_path(
    client: ClientName,
    *,
    home_dir: Path | None = None,
    workspace_dir: Path | None = None,
    project_scoped: bool = False,
) -> Path:
    """Resolve the config file path for a supported MCP client."""
    home = (home_dir or Path.home()).expanduser()
    ws = (workspace_dir or Path.cwd()).expanduser()

    if client == "antigravity":
        return home / ".gemini" / "antigravity" / "mcp_config.json"
    if client == "claude_code":
        return (ws / ".mcp.json") if project_scoped else (home / ".claude.json")
    if client == "codex":
        return home / ".codex" / "config.toml"
    if client == "cursor":
        return (ws / ".cursor" / "mcp.json") if project_scoped else (home / ".cursor" / "mcp.json")
    if client == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    if client == "gemini_cli":
        return (ws / ".gemini" / "settings.json") if project_scoped else (home / ".gemini" / "settings.json")
    raise ValueError(f"Unsupported client: {client}")


def _next_backup_path(target_path: Path) -> Path:
    base_bak = target_path.with_name(f"{target_path.name}.bak")
    if not base_bak.exists():
        return base_bak
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return target_path.with_name(f"{target_path.name}.{stamp}.bak")


def _merge_json_config(
    target_path: Path,
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> str:
    data: dict[str, Any] = {}
    if target_path.is_file():
        raw = target_path.read_text(encoding="utf-8-sig").strip()
        if raw:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError(f"Expected JSON object in {target_path}")
            data = loaded

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["omnimex"] = build_json_mcp_entry(command, args, env=env)
    data["mcpServers"] = servers
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _merge_codex_toml(
    target_path: Path,
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> str:
    block = build_codex_toml_block(command, args, env=env)
    if not target_path.is_file():
        return block

    raw = target_path.read_text(encoding="utf-8-sig")
    if raw.strip():
        # Validate existing TOML before modifying
        tomllib.loads(raw)

    if _CODEX_SECTION_RE.search(raw):
        updated = _CODEX_SECTION_RE.sub(lambda _: block, raw, count=1)
    else:
        sep = "\n\n" if raw.strip() and not raw.endswith("\n\n") else ("\n" if raw.strip() and not raw.endswith("\n") else "")
        updated = f"{raw}{sep}{block}"

    # Verify generated TOML parses cleanly
    tomllib.loads(updated)
    return updated


def _merge_rules_markdown(target_path: Path) -> str:
    if not target_path.is_file():
        return OMNIMEX_AGENT_RULES_SNIPPET
    raw = target_path.read_text(encoding="utf-8-sig")
    if _RULES_BLOCK_RE.search(raw):
        return _RULES_BLOCK_RE.sub(OMNIMEX_AGENT_RULES_SNIPPET, raw, count=1)
    sep = "\n\n" if raw.strip() and not raw.endswith("\n\n") else ("\n" if raw.strip() and not raw.endswith("\n") else "")
    return f"{raw}{sep}{OMNIMEX_AGENT_RULES_SNIPPET}"


def write_file_with_backup(target_path: Path, content: str) -> Path | None:
    """Write text atomically to *target_path*, creating a timestamped .bak if it exists."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target_path.is_file():
        backup_path = _next_backup_path(target_path)
        shutil.copy2(target_path, backup_path)
    target_path.write_text(content, encoding="utf-8")
    return backup_path


_write_with_backup = write_file_with_backup


def _plan_or_apply_file(
    *,
    client: str,
    target_path: Path,
    fmt: Literal["json", "toml", "markdown"],
    preview: str,
    apply: bool,
) -> SetupActionResult:
    exists = target_path.is_file()
    planned_backup = _next_backup_path(target_path) if exists else None
    actual_backup = _write_with_backup(target_path, preview) if apply else None
    return SetupActionResult(
        client=client,
        target_path=target_path,
        format=fmt,
        would_create=not exists,
        would_backup=exists,
        backup_path=actual_backup if apply else planned_backup,
        preview_content=preview,
        applied=apply,
    )


def configure_mcp_clients(
    clients: Sequence[ClientName] | None = None,
    *,
    apply: bool = False,
    home_dir: Path | None = None,
    workspace_dir: Path | None = None,
    project_scoped: bool = False,
    command: str | None = None,
    omnimex_config_path: Path | None = None,
    target_overrides: dict[str, Path] | None = None,
) -> SetupReport:
    """Generate (or apply with ``.bak`` backup when ``apply=True``) MCP client configs."""
    selected = list(clients) if clients is not None else list(SUPPORTED_MCP_CLIENTS)
    resolved_cmd, resolved_args = resolve_omnimex_command(command)
    env = {"OMNIMEX_CONFIG": str(omnimex_config_path)} if omnimex_config_path else None

    report = SetupReport(apply=apply)
    for client in selected:
        target = (
            target_overrides[client]
            if target_overrides and client in target_overrides
            else default_client_config_path(
                client,
                home_dir=home_dir,
                workspace_dir=workspace_dir,
                project_scoped=project_scoped,
            )
        )
        fmt: Literal["json", "toml"] = "toml" if client == "codex" else "json"
        try:
            preview = (
                _merge_codex_toml(target, resolved_cmd, resolved_args, env=env)
                if fmt == "toml"
                else _merge_json_config(target, resolved_cmd, resolved_args, env=env)
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{client} ({target}): {exc}")
            continue

        report.actions.append(
            _plan_or_apply_file(
                client=client,
                target_path=target,
                fmt=fmt,
                preview=preview,
                apply=apply,
            )
        )
    return report


def export_agent_rules(
    target_paths: Sequence[Path],
    *,
    apply: bool = False,
) -> SetupReport:
    """Export/inject the Omnimex memory workflow snippet into AGENTS.md / CLAUDE.md."""
    report = SetupReport(apply=apply)
    for target in target_paths:
        preview = _merge_rules_markdown(target)
        report.actions.append(
            _plan_or_apply_file(
                client="rules",
                target_path=target,
                fmt="markdown",
                preview=preview,
                apply=apply,
            )
        )
    return report


def install_skill(
    skill_source: Path,
    *,
    home_dir: Path | None = None,
    target_dirs: Sequence[Path] | None = None,
    apply: bool = False,
) -> SetupReport:
    """Install the ``omnimex-memory`` SKILL.md into agent skill directories."""
    if not skill_source.is_file():
        raise FileNotFoundError(f"Skill file not found: {skill_source}")
    content = skill_source.read_text(encoding="utf-8")
    home = (home_dir or Path.home()).expanduser()
    destinations = (
        list(target_dirs)
        if target_dirs is not None
        else [
            home / ".codex" / "skills" / "omnimex-memory" / "SKILL.md",
            home / ".claude" / "skills" / "omnimex-memory" / "SKILL.md",
            home / ".gemini" / "antigravity" / "skills" / "omnimex-memory" / "SKILL.md",
        ]
    )
    report = SetupReport(apply=apply)
    for dest in destinations:
        target = dest / "SKILL.md" if dest.is_dir() else dest
        report.actions.append(
            _plan_or_apply_file(
                client="skill",
                target_path=target,
                fmt="markdown",
                preview=content,
                apply=apply,
            )
        )
    return report


__all__ = [
    "ClientName",
    "SUPPORTED_MCP_CLIENTS",
    "SetupActionResult",
    "SetupReport",
    "configure_mcp_clients",
    "default_client_config_path",
    "export_agent_rules",
    "install_skill",
    "write_file_with_backup",
]
