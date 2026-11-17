"""Mainstream coding-agent session adapters, registry, and Watchdog file watcher."""

from omnimex.adapters.antigravity import AntigravityAdapter
from omnimex.adapters.base import (
    AdapterSourceStatus,
    BaseAgentAdapter,
    HealthState,
    IncrementalParseResult,
)
from omnimex.adapters.claude_code import ClaudeCodeAdapter
from omnimex.adapters.cline_roo import ClineAdapter, RooCodeAdapter
from omnimex.adapters.codex import CodexAdapter
from omnimex.adapters.copilot import CopilotAdapter
from omnimex.adapters.cursor import CursorAdapter
from omnimex.adapters.gemini_cli import GeminiCliAdapter
from omnimex.adapters.opencode_aider import AiderAdapter, OpenCodeAdapter
from omnimex.adapters.registry import AdapterRegistry, create_default_registry
from omnimex.adapters.watcher import DEFAULT_DEBOUNCE_MS, SessionWatcher, WatchEvent
from omnimex.adapters.windsurf import WindsurfAdapter

__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "AdapterRegistry",
    "AdapterSourceStatus",
    "AiderAdapter",
    "AntigravityAdapter",
    "BaseAgentAdapter",
    "ClaudeCodeAdapter",
    "ClineAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "CursorAdapter",
    "GeminiCliAdapter",
    "HealthState",
    "IncrementalParseResult",
    "OpenCodeAdapter",
    "RooCodeAdapter",
    "SessionWatcher",
    "WatchEvent",
    "WindsurfAdapter",
    "create_default_registry",
]
