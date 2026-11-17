"""Typer CLI entrypoints for Omnimex."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from omnimex.api.app import create_app
from omnimex.core.config import DistillerConfig, find_config_file, load_config
from omnimex.core.models import SearchMode
from omnimex.mcp_server.server import McpTransport, run_mcp_server
from omnimex.memory.watcher import CentralMemoryWatcher
from omnimex.services.ingest import IngestService
from omnimex.setup.installer import (
    SUPPORTED_MCP_CLIENTS,
    ClientName,
    configure_mcp_clients,
    export_agent_rules,
    install_skill,
)

app = typer.Typer(
    name="omnimex",
    no_args_is_help=True,
    help="Omnimex: Cross-agent session search, central Markdown memory hub, and handoff CLI.",
)


def _load_service(config_path: Path | None) -> IngestService:
    cfg = load_config(config_path) if config_path is not None else load_config()
    return IngestService(cfg)


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _default_skill_path() -> Path:
    return Path(__file__).resolve().parents[2] / "skills" / "omnimex-memory" / "SKILL.md"


@app.command()
def status(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON output.")] = False,
) -> None:
    """Show Omnimex database, memory hub, distiller, and adapter discovery health."""
    service = _load_service(config)
    try:
        stats = service.get_stats()
        adapters = [asdict(a) for a in service.get_adapter_statuses()]
        payload = {
            "db_path": str(service.config.db_path),
            "memories_dir": str(service.config.memories_dir),
            "embedding": {
                "model": service.embedder.model_name,
                "dimension": service.embedder.dimension,
                "offline_fallback": service.embedder.is_fallback,
            },
            "stats": stats,
            "adapters": adapters,
        }
        if json_output:
            _echo_json(payload)
            return

        typer.echo(f"DB Path:       {payload['db_path']}")
        typer.echo(f"Memories Dir:  {payload['memories_dir']}")
        typer.echo(
            f"Embedder:      {service.embedder.model_name} "
            f"({'offline-fallback' if service.embedder.is_fallback else 'local-onnx'})"
        )
        dist = stats["distiller"]
        typer.echo(
            f"Distiller:     {dist['provider']} (zero_cost_mode={dist['zero_cost_mode']}, "
            f"pending={dist['pending_sessions_count']})"
        )
        typer.echo(
            f"Totals:        {stats['total_sessions']} sessions, {stats['total_turns']} turns, "
            f"{stats['total_memories']} memories, {stats['index_embeddings_count']} embeddings"
        )
        typer.echo("\nAdapters:")
        for ad in adapters:
            typer.echo(
                f"  - {ad['id']:<12} [{ad['status']:<17}] files={ad['discovered_files']:<3} "
                f"sessions={ad['session_count']:<3} turns={ad['turn_count']}"
            )
    finally:
        service.close()


@app.command()
def scan(
    agent: Annotated[list[str] | None, typer.Option("--agent", "-a", help="Filter to specific agent(s).")] = None,
    full: Annotated[bool, typer.Option("--full", help="Ignore cursors and perform full rescan.")] = False,
    distill: Annotated[
        bool | None,
        typer.Option("--distill/--no-distill", help="Override config auto_distill_on_scan."),
    ] = None,
    import_native: Annotated[
        bool, typer.Option("--native-import/--no-native-import", help="Import native agent memories read-only.")
    ] = True,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON output.")] = False,
) -> None:
    """Scan agent transcripts incrementally, index FTS5 + vectors, and optionally distill."""
    service = _load_service(config)
    try:
        res = service.scan(
            adapters=agent,
            full_rescan=full,
            distill=distill,
            import_native=import_native,
        )
        payload = asdict(res)
        if json_output:
            _echo_json(payload)
            return
        typer.echo(
            f"Scan {res.job_id} complete in {res.duration_ms}ms: "
            f"files={res.scanned_files}, new_sessions={res.new_sessions}, "
            f"updated_sessions={res.updated_sessions}, new_turns={res.new_turns}, "
            f"distilled_memories={res.distilled_memories}, native_imported={res.imported_native_memories}"
        )
        for err in res.errors:
            typer.echo(f"  [warn] {err.agent} ({err.path}): {err.error}", err=True)
    finally:
        service.close()


@app.command()
def watch(
    debounce_ms: Annotated[int | None, typer.Option("--debounce-ms", help="Override Watchdog debounce ms.")] = None,
    initial_scan: Annotated[
        bool, typer.Option("--initial-scan/--no-initial-scan", help="Run an incremental scan before watching.")
    ] = True,
    watch_memories: Annotated[
        bool, typer.Option("--watch-memories/--no-watch-memories", help="Watch central data/memories/*.md edits.")
    ] = True,
    duration: Annotated[
        float | None, typer.Option("--duration", help="Optional seconds to run before stopping cleanly.")
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
) -> None:
    """Watch agent transcript folders and central Markdown memories in real time."""
    service = _load_service(config)
    if debounce_ms is not None:
        service.config.adapters.watch_debounce_ms = debounce_ms

    memory_watcher: CentralMemoryWatcher | None = None
    try:
        if initial_scan:
            res = service.scan()
            typer.echo(
                f"Initial scan: files={res.scanned_files}, new_sessions={res.new_sessions}, new_turns={res.new_turns}"
            )
        service.start_watcher()
        if watch_memories:
            memory_watcher = CentralMemoryWatcher(
                service.memory_manager,
                debounce_ms=service.config.adapters.watch_debounce_ms,
            )
            memory_watcher.start()

        typer.echo("Watching agent sources and central memories (Ctrl+C to stop)...")
        deadline = (time.monotonic() + duration) if duration is not None else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        typer.echo("\nStopping watchers...")
    finally:
        if memory_watcher is not None:
            memory_watcher.stop()
        service.close()


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query (empty string browses recent turns).")] = "",
    agent: Annotated[list[str] | None, typer.Option("--agent", "-a", help="Filter by agent slug(s).")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", "-w", help="Filter by workspace path.")] = None,
    mode: Annotated[SearchMode, typer.Option("--mode", "-m", help="Search mode: hybrid, fts, or vector.")] = "hybrid",
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=100)] = 10,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON output.")] = False,
) -> None:
    """Search cross-agent sessions using FTS5 BM25 + vector RRF fusion."""
    service = _load_service(config)
    try:
        page = service.searcher.search_page(
            query,
            agents=agent,
            workspace=workspace,
            mode=mode,
            limit=limit,
        )
        if json_output:
            _echo_json(
                {
                    "query": page.query,
                    "mode": page.mode,
                    "total": page.total,
                    "took_ms": page.took_ms,
                    "hits": [h.model_dump(mode="json") for h in page.hits],
                    "facets": page.facets,
                }
            )
            return

        typer.echo(f"Found {page.total} hit(s) in {page.took_ms}ms [mode={page.mode}]:")
        for hit in page.hits:
            typer.echo(
                f"  [{hit.score:.3f}] {hit.agent} | {hit.session_id}#t{hit.turn_index} ({hit.role})\n"
                f"          {hit.snippet}"
            )
    finally:
        service.close()


@app.command()
def distill(
    session: Annotated[str | None, typer.Option("--session", "-s", help="Distill a single session ID.")] = None,
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Filter backfill by agent.")] = None,
    backfill: Annotated[bool, typer.Option("--backfill/--no-backfill", help="Run backfill over pending sessions.")] = True,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 25,
    force: Annotated[bool, typer.Option("--force", help="Re-distill already processed sessions.")] = False,
    provider: Annotated[str | None, typer.Option("--provider", help="Override distiller provider.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Override distiller model.")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Override distiller base URL.")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON output.")] = False,
) -> None:
    """Distill sessions into central Markdown memories or backfill with another provider."""
    service = _load_service(config)
    try:
        if provider or model or base_url:
            current = service.config.distiller
            next_cfg = DistillerConfig(
                provider=provider or current.provider,  # type: ignore[arg-type]
                model=model or current.model,
                base_url=base_url or current.base_url,
                api_key=current.api_key,
                auto_distill_on_scan=current.auto_distill_on_scan,
                timeout_seconds=current.timeout_seconds,
            )
            service.distillation.set_distiller(next_cfg)

        if session:
            try:
                outcome = service.distillation.distill_session(session, force=force)
            except LookupError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
            payload = {
                "session_id": outcome.session_id,
                "provider": outcome.provider,
                "skipped": outcome.skipped,
                "created_memories": [m.model_dump(mode="json") for m in outcome.created_memories],
            }
            if json_output:
                _echo_json(payload)
            else:
                typer.echo(
                    f"Session {outcome.session_id}: provider={outcome.provider}, "
                    f"skipped={outcome.skipped}, created={len(outcome.created_memories)}"
                )
            return

        if not backfill:
            typer.echo("Pass --session <id> or --backfill.", err=True)
            raise typer.Exit(code=2)

        job_id, res = service.backfill_distill(limit=limit, agent=agent, force=force)
        payload = {"job_id": job_id, **asdict(res)}
        if json_output:
            _echo_json(payload)
        else:
            typer.echo(
                f"Backfill {job_id} ({res.provider}): processed={res.processed_sessions}, "
                f"created_memories={res.created_memories}, skipped={res.skipped_sessions}"
            )
    finally:
        service.close()


@app.command()
def handoff(
    session_id: Annotated[str, typer.Argument(help="Source NormalizedSession ID.")],
    target: Annotated[str, typer.Option("--target", "-t", help="Target coding agent slug.")] = "generic",
    include_memories: Annotated[bool, typer.Option("--memories/--no-memories")] = True,
    include_tools: Annotated[bool, typer.Option("--tools/--no-tools")] = True,
    max_tokens: Annotated[int, typer.Option("--max-tokens", min=200, max=8000)] = 1200,
    instructions: Annotated[str | None, typer.Option("--instructions", "-i")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write Markdown brief to file.")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON HandoffBrief.")] = False,
) -> None:
    """Generate a redacted cross-agent handoff brief from a session."""
    service = _load_service(config)
    try:
        try:
            brief = service.handoff_builder.build(
                session_id,
                target_agent=target,
                include_memories=include_memories,
                include_tool_summary=include_tools,
                max_tokens=max_tokens,
                custom_instructions=instructions,
            )
        except LookupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(brief.markdown, encoding="utf-8")

        if json_output:
            _echo_json(brief.model_dump(mode="json"))
        else:
            typer.echo(brief.markdown)
    finally:
        service.close()


@app.command()
def mcp(
    transport: Annotated[
        McpTransport,
        typer.Option("--transport", help="MCP transport: stdio or streamable-http."),
    ] = "stdio",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8766,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
) -> None:
    """Start the FastMCP server exposing progressive history, memory, and handoff tools."""
    cfg = load_config(config) if config is not None else load_config()
    run_mcp_server(transport=transport, host=host, port=port, config=cfg)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option("--host", help="Bind host (defaults to omnimex.toml).")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port (defaults to omnimex.toml).")] = None,
    static: Annotated[bool, typer.Option("--static/--no-static", help="Serve built frontend/dist when available.")] = True,
    scan_on_startup: Annotated[bool | None, typer.Option("--scan/--no-scan")] = None,
    watch: Annotated[bool | None, typer.Option("--watch/--no-watch")] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Enable Uvicorn auto-reload.")] = False,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
) -> None:
    """Start the Omnimex FastAPI REST/SSE server + Web UI."""
    cfg = load_config(config) if config is not None else load_config()
    bind_host = host or cfg.app.host
    bind_port = port or cfg.app.port
    if reload:
        import os

        if config is not None:
            os.environ["OMNIMEX_CONFIG"] = str(config.resolve())
        uvicorn.run(
            "omnimex.api.app:create_app",
            factory=True,
            host=bind_host,
            port=bind_port,
            log_level=cfg.app.log_level,
            reload=True,
        )
        return

    fastapi_app = create_app(
        cfg,
        serve_static=static,
        start_watcher=watch,
        scan_on_startup=scan_on_startup,
    )
    uvicorn.run(
        fastapi_app,
        host=bind_host,
        port=bind_port,
        log_level=cfg.app.log_level,
    )


@app.command()
def tray(
    host: Annotated[str | None, typer.Option("--host", help="Bind host (defaults to omnimex.toml).")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port (defaults to omnimex.toml).")] = None,
    scan_on_startup: Annotated[bool | None, typer.Option("--scan/--no-scan")] = None,
    watch: Annotated[bool | None, typer.Option("--watch/--no-watch")] = None,
    open_window: Annotated[bool, typer.Option("--open/--no-open", help="Open standalone app window on launch.")] = True,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Path to omnimex.toml.")] = None,
) -> None:
    """Start Omnimex in system tray + standalone browser app-window mode."""
    from omnimex.tray import run_tray

    cfg = load_config(config) if config is not None else load_config()
    code = run_tray(
        config=cfg,
        host=host,
        port=port,
        scan_on_startup=scan_on_startup,
        watch=watch,
        open_on_start=open_window,
    )
    if code != 0:
        raise typer.Exit(code=code)


@app.command()
def setup(
    client: Annotated[
        list[str] | None,
        typer.Option(
            "--client",
            help=f"Target MCP client(s): {', '.join(SUPPORTED_MCP_CLIENTS)}.",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Write files (creating .bak backups) instead of previewing."),
    ] = False,
    project: Annotated[bool, typer.Option("--project", help="Write project-scoped configs where supported.")] = False,
    export_rules: Annotated[
        bool, typer.Option("--export-rules", help="Export/update AGENTS.md and CLAUDE.md snippets.")
    ] = False,
    install_skills: Annotated[
        bool, typer.Option("--install-skill", help="Install omnimex-memory SKILL.md to agent skill dirs.")
    ] = False,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    home: Annotated[Path | None, typer.Option("--home")] = None,
    command: Annotated[str | None, typer.Option("--command", help="Override omnimex command in MCP config.")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Optional OMNIMEX_CONFIG env path.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON setup report.")] = False,
) -> None:
    """Preview or apply safe MCP client configuration (.bak backups created on --apply)."""
    selected_clients: list[ClientName] | None = None
    if client:
        selected_clients = []
        for item in client:
            for slug in item.split(","):
                clean = slug.strip().lower()
                if not clean:
                    continue
                if clean not in SUPPORTED_MCP_CLIENTS:
                    typer.echo(
                        f"Unsupported client '{clean}'. Supported: {', '.join(SUPPORTED_MCP_CLIENTS)}",
                        err=True,
                    )
                    raise typer.Exit(code=2)
                selected_clients.append(clean)  # type: ignore[arg-type]

    ws_dir = (workspace or Path.cwd()).expanduser()
    resolved_config = config.expanduser().resolve() if config is not None else find_config_file(ws_dir)
    mcp_report = configure_mcp_clients(
        selected_clients,
        apply=apply,
        home_dir=home,
        workspace_dir=ws_dir,
        project_scoped=project,
        command=command,
        omnimex_config_path=resolved_config,
    )
    all_actions = list(mcp_report.actions)
    all_errors = list(mcp_report.errors)

    if export_rules:
        rules_report = export_agent_rules(
            [ws_dir / "AGENTS.md", ws_dir / "CLAUDE.md"],
            apply=apply,
        )
        all_actions.extend(rules_report.actions)
        all_errors.extend(rules_report.errors)

    if install_skills:
        skill_src = _default_skill_path()
        skill_report = install_skill(skill_src, home_dir=home, apply=apply)
        all_actions.extend(skill_report.actions)
        all_errors.extend(skill_report.errors)

    if json_output:
        _echo_json(
            {
                "apply": apply,
                "errors": all_errors,
                "actions": [
                    {
                        "client": a.client,
                        "target_path": str(a.target_path),
                        "format": a.format,
                        "would_create": a.would_create,
                        "would_backup": a.would_backup,
                        "backup_path": str(a.backup_path) if a.backup_path else None,
                        "applied": a.applied,
                        "preview_content": a.preview_content,
                    }
                    for a in all_actions
                ],
            }
        )
    else:
        mode_label = "APPLIED" if apply else "DRY-RUN (pass --apply to write)"
        typer.echo(f"Omnimex setup [{mode_label}]:")
        for action in all_actions:
            verb = "wrote" if action.applied else ("create" if action.would_create else "update")
            bak_note = f" (backup: {action.backup_path})" if action.backup_path else ""
            typer.echo(f"  - [{action.client}] {verb} {action.target_path}{bak_note}")
        for err in all_errors:
            typer.echo(f"  [error] {err}", err=True)

    if all_errors:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
