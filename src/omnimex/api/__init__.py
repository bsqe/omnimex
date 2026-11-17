"""Omnimex FastAPI web & SSE interface."""

from __future__ import annotations

from omnimex.api.app import create_app
from omnimex.api.schemas import ApiError

__all__ = ["ApiError", "create_app"]
