"""MCP client setup generator/installer and AGENTS.md/CLAUDE.md rule exporter."""

from omnimex.setup.installer import (
    SUPPORTED_MCP_CLIENTS,
    ClientName,
    SetupActionResult,
    SetupReport,
    configure_mcp_clients,
    default_client_config_path,
    export_agent_rules,
    install_skill,
)
from omnimex.setup.templates import (
    OMNIMEX_AGENT_RULES_SNIPPET,
    build_codex_toml_block,
    build_json_mcp_entry,
    resolve_omnimex_command,
)

__all__ = [
    "ClientName",
    "OMNIMEX_AGENT_RULES_SNIPPET",
    "SUPPORTED_MCP_CLIENTS",
    "SetupActionResult",
    "SetupReport",
    "build_codex_toml_block",
    "build_json_mcp_entry",
    "configure_mcp_clients",
    "default_client_config_path",
    "export_agent_rules",
    "install_skill",
    "resolve_omnimex_command",
]
