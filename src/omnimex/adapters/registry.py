"""Adapter registry and path routing across all Omnimex coding-agent adapters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

from omnimex.adapters.antigravity import AntigravityAdapter
from omnimex.adapters.base import (
    AdapterSourceStatus,
    BaseAgentAdapter,
    normalize_source_path,
    safe_is_dir,
    safe_is_file,
)
from omnimex.adapters.claude_code import ClaudeCodeAdapter
from omnimex.adapters.cline_roo import ClineAdapter, RooCodeAdapter
from omnimex.adapters.codex import CodexAdapter
from omnimex.adapters.copilot import CopilotAdapter
from omnimex.adapters.cursor import CursorAdapter
from omnimex.adapters.gemini_cli import GeminiCliAdapter
from omnimex.adapters.opencode_aider import AiderAdapter, OpenCodeAdapter
from omnimex.adapters.windsurf import WindsurfAdapter
from omnimex.core.models import MemoryCard, NormalizedSession


class AdapterRegistry:
    """Registry managing all configured coding-agent adapters."""

    def __init__(self, adapters: Sequence[BaseAgentAdapter] = ()) -> None:
        self._adapters: dict[str, BaseAgentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: BaseAgentAdapter) -> None:
        """Register (or replace) an adapter instance by its ``agent_id``."""
        if not adapter.agent_id:
            raise ValueError("Adapter must define a non-empty agent_id")
        self._adapters[adapter.agent_id] = adapter

    def get(self, agent_id: str) -> BaseAgentAdapter | None:
        """Return registered adapter for ``agent_id``, or None."""
        return self._adapters.get(agent_id)

    def require(self, agent_id: str) -> BaseAgentAdapter:
        """Return registered adapter for ``agent_id`` or raise KeyError."""
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            raise KeyError(f"Unknown agent adapter: {agent_id!r}")
        return adapter

    def agent_ids(self) -> list[str]:
        """Return registered adapter IDs in registration order."""
        return list(self._adapters.keys())

    def adapters(self, enabled: Sequence[str] | None = None) -> list[BaseAgentAdapter]:
        """Return registered adapters, optionally filtered to ``enabled`` IDs."""
        if enabled is None:
            return list(self._adapters.values())
        allowed = set(enabled)
        return [a for a in self._adapters.values() if a.agent_id in allowed]

    def detect_all_sources(
        self, enabled: Sequence[str] | None = None
    ) -> dict[str, list[Path]]:
        """Discover source files per enabled adapter."""
        return {
            adapter.agent_id: adapter.detect_sources()
            for adapter in self.adapters(enabled)
        }

    def iter_all_sessions(
        self,
        enabled: Sequence[str] | None = None,
        *,
        since: datetime | None = None,
    ) -> Iterator[NormalizedSession]:
        """Stream normalized sessions across all enabled adapters."""
        for adapter in self.adapters(enabled):
            yield from adapter.iter_sessions(since=since)

    def discover_all_native_memories(
        self, enabled: Sequence[str] | None = None
    ) -> list[MemoryCard]:
        """Collect native memory cards across enabled adapters."""
        cards: list[MemoryCard] = []
        for adapter in self.adapters(enabled):
            if adapter.supports_native_memories:
                cards.extend(adapter.discover_native_memories())
        return cards

    def health_report(
        self, enabled: Sequence[str] | None = None
    ) -> list[AdapterSourceStatus]:
        """Return discovery and permission diagnostics for all enabled adapters."""
        return [adapter.health_status() for adapter in self.adapters(enabled)]

    def watch_directories(self, enabled: Sequence[str] | None = None) -> list[Path]:
        """Collect unique existing watchable root directories across enabled adapters."""
        dirs: list[Path] = []
        seen: set[str] = set()
        for adapter in self.adapters(enabled):
            for root in adapter.resolve_roots():
                target = root.parent if safe_is_file(root) else root
                if not safe_is_dir(target):
                    continue
                key = normalize_source_path(target).lower()
                if key not in seen:
                    seen.add(key)
                    dirs.append(target)
        return dirs

    def resolve_adapter_for_path(
        self,
        path: Path,
        enabled: Sequence[str] | None = None,
    ) -> tuple[BaseAgentAdapter, Path] | None:
        """Map a filesystem path (including SQLite ``-wal``/``-shm``) to an adapter."""
        candidate = path
        name_lower = path.name.lower()
        for suffix in ("-wal", "-shm"):
            if name_lower.endswith(suffix):
                primary = Path(str(path)[: -len(suffix)])
                if safe_is_file(primary):
                    candidate = primary
                break

        for adapter in self.adapters(enabled):
            if adapter.matches_path(candidate):
                return adapter, candidate
        return None


def create_default_registry(
    *,
    enabled: Sequence[str] | None = None,
) -> AdapterRegistry:
    """Create an ``AdapterRegistry`` containing all 11 mainstream adapters."""
    all_adapters: list[BaseAgentAdapter] = [
        AntigravityAdapter(),
        ClaudeCodeAdapter(),
        CodexAdapter(),
        CursorAdapter(),
        GeminiCliAdapter(),
        WindsurfAdapter(),
        ClineAdapter(),
        RooCodeAdapter(),
        CopilotAdapter(),
        OpenCodeAdapter(),
        AiderAdapter(),
    ]
    if enabled is not None:
        allowed = set(enabled)
        all_adapters = [a for a in all_adapters if a.agent_id in allowed]
    return AdapterRegistry(all_adapters)


__all__ = ["AdapterRegistry", "create_default_registry"]
