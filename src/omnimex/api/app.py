"""FastAPI application factory, exception envelope handlers, CORS, and SPA static mount."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from omnimex import __version__
from omnimex.api.routes import router as api_router
from omnimex.api.schemas import ApiError
from omnimex.core.config import OmnimexConfig, load_config
from omnimex.services.ingest import IngestService

logger = logging.getLogger(__name__)

_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def _status_to_code(status_code: int) -> str:
    mapping = {
        400: "VALIDATION_ERROR",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        502: "DISTILLER_ERROR",
    }
    return mapping.get(status_code, "INTERNAL_ERROR")


def _resolve_static_dist_dir(config: OmnimexConfig) -> Path | None:
    repo_root = Path(__file__).resolve().parents[3]
    for root in (config.root_dir, repo_root):
        candidate = root / "frontend" / "dist"
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


def _mount_static_frontend(app: FastAPI, dist_dir: Path) -> None:
    resolved_dist = dist_dir.resolve()
    assets_dir = resolved_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    index_file = resolved_dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise ApiError(404, "NOT_FOUND", f"API endpoint '/{full_path}' not found")
        if full_path:
            target = (resolved_dist / full_path).resolve()
            try:
                target.relative_to(resolved_dist)
            except ValueError:
                raise ApiError(403, "PERMISSION_DENIED", "Forbidden static path") from None
            if target.is_file():
                return FileResponse(target)
        return FileResponse(index_file)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, details=exc.details)

    @app.exception_handler(LookupError)
    async def handle_lookup_error(_request: Request, exc: LookupError) -> JSONResponse:
        return _error_response(404, "NOT_FOUND", str(exc) or "Resource not found")

    @app.exception_handler(PermissionError)
    async def handle_permission_error(_request: Request, exc: PermissionError) -> JSONResponse:
        return _error_response(403, "PERMISSION_DENIED", str(exc) or "Permission denied")

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return _error_response(400, "VALIDATION_ERROR", str(exc) or "Invalid request value")

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "Request validation failed",
            details={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            exc.status_code,
            _status_to_code(exc.status_code),
            str(exc.detail) if exc.detail else "HTTP error",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            500,
            "INTERNAL_ERROR",
            f"Internal server error: {exc}",
        )


def create_app(
    config: OmnimexConfig | None = None,
    *,
    ingest_service: IngestService | None = None,
    serve_static: bool = True,
    start_watcher: bool | None = None,
    scan_on_startup: bool | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Create the configured Omnimex FastAPI application."""
    cfg = config or (ingest_service.config if ingest_service is not None else load_config())
    service = ingest_service or IngestService(cfg)
    owns_service = ingest_service is None

    should_watch = cfg.adapters.watch_enabled if start_watcher is None else start_watcher
    should_scan = cfg.adapters.scan_on_startup if scan_on_startup is None else scan_on_startup

    @asynccontextmanager
    async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
        service.events.reopen()
        stop_bg = threading.Event()
        startup_thread: threading.Thread | None = None

        if should_watch:
            service.start_watcher()

        if should_scan:
            def _run_startup_scan() -> None:
                if stop_bg.is_set():
                    return
                try:
                    service.scan()
                except Exception:  # noqa: BLE001
                    logger.exception("Background startup scan failed")

            startup_thread = threading.Thread(
                target=_run_startup_scan,
                name="omnimex-startup-scan",
                daemon=True,
            )
            application.state.startup_scan_thread = startup_thread
            startup_thread.start()
        else:
            application.state.startup_scan_thread = None

        try:
            yield
        finally:
            stop_bg.set()
            service.events.close()
            if owns_service:
                service.close()
            elif should_watch:
                service.stop_watcher()
            if startup_thread is not None and startup_thread.is_alive():
                startup_thread.join(timeout=1.5)

    app = FastAPI(
        title="Omnimex API",
        version=__version__,
        description="Unified coding-agent session memory & handoff REST/SSE API",
        lifespan=_lifespan,
    )
    app.state.ingest_service = service
    app.state.config = cfg
    app.state.sse_heartbeat_seconds = 15.0

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app)
    app.include_router(api_router)

    if serve_static:
        dist_dir = static_dir or _resolve_static_dist_dir(cfg)
        if dist_dir is not None and dist_dir.is_dir() and (dist_dir / "index.html").is_file():
            _mount_static_frontend(app, dist_dir)

    return app
