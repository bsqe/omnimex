"""FastMCP server exposing Omnimex progressive search, memory, and handoff tools."""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from omnimex.core.config import OmnimexConfig, load_config
from omnimex.mcp_server.tools import OmnimexMcpTools
from omnimex.memory.markdown_store import MemoryManager
from omnimex.storage.db import OmnimexDB
from omnimex.storage.embeddings import EmbeddingProvider

McpTransport = Literal["stdio", "streamable-http", "http", "sse"]

_MCP_INSTRUCTIONS = (
    "Omnimex Cross-Agent Memory Hub. Use progressive disclosure: "
    "start with memory_recall / history_search to get compact summaries and IDs, "
    "then call memory_read / history_read only for items you need. "
    "Use memory_save to persist non-trivial fixes, architectural decisions, and user preferences "
    "to Omnimex's central Markdown memory store."
)


def create_mcp_server(
    config: OmnimexConfig | None = None,
    *,
    db: OmnimexDB | None = None,
    embedder: EmbeddingProvider | None = None,
    memory_manager: MemoryManager | None = None,
    tools: OmnimexMcpTools | None = None,
) -> tuple[FastMCP, OmnimexMcpTools]:
    """Build a configured ``FastMCP`` instance and its bound ``OmnimexMcpTools``."""
    toolset = tools or OmnimexMcpTools.from_config(
        config,
        db=db,
        embedder=embedder,
        memory_manager=memory_manager,
    )
    mcp = FastMCP(
        name="Omnimex",
        instructions=_MCP_INSTRUCTIONS,
        version="0.1.0",
    )

    mcp.tool(
        name="history_search",
        description="Search cross-agent session history (FTS5 + vector RRF). Returns compact snippets and session IDs.",
    )(toolset.history_search)

    mcp.tool(
        name="history_read",
        description="Read a paginated turn window from a specific session ID.",
    )(toolset.history_read)

    mcp.tool(
        name="memory_recall",
        description="Recall compact summaries and IDs of central Markdown memories.",
    )(toolset.memory_recall)

    mcp.tool(
        name="memory_read",
        description="Read the full central Markdown memory card by memory_id.",
    )(toolset.memory_read)

    mcp.tool(
        name="memory_save",
        description="Save a durable preference, decision, pitfall, or fact to Omnimex central Markdown memories.",
    )(toolset.memory_save)

    mcp.tool(
        name="memory_update",
        description="Update an existing central Markdown memory card by memory_id.",
    )(toolset.memory_update)

    mcp.tool(
        name="session_handoff",
        description="Generate a redacted cross-agent handoff brief from a session and its relevant memories.",
    )(toolset.session_handoff)

    return mcp, toolset


def run_mcp_server(
    transport: McpTransport = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    config: OmnimexConfig | None = None,
) -> None:
    """Start the FastMCP server over ``stdio`` or ``streamable-http``."""
    cfg = config or load_config()
    mcp, toolset = create_mcp_server(cfg)
    try:
        if transport == "stdio":
            mcp.run(transport="stdio", show_banner=False)
        else:
            mcp.run(transport=transport, host=host, port=port, show_banner=False)
    finally:
        toolset.db.close()


__all__ = ["McpTransport", "create_mcp_server", "run_mcp_server"]
