"""Configuration loader and models for Omnimex."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimex.core.redaction import redact_config, redact_url

EmbeddingProviderKind = Literal["auto", "fastembed", "offline_hash"]
DistillerProviderKind = Literal["heuristic", "ollama", "openai_compatible"]

DEFAULT_ENABLED_ADAPTERS: list[str] = [
    "antigravity",
    "claude_code",
    "codex",
    "cursor",
    "gemini_cli",
    "windsurf",
    "cline",
    "roo_code",
    "copilot",
    "opencode",
    "aider",
]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = "info"


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    db_path: Path = Path("data/omnimex.db")
    memories_dir: Path = Path("data/memories")
    wal_mode: bool = True
    busy_timeout_ms: int = Field(default=5000, ge=0)

    def resolve_db_path(self, root: Path) -> Path:
        return self.db_path if self.db_path.is_absolute() else (root / self.db_path)

    def resolve_memories_dir(self, root: Path) -> Path:
        return self.memories_dir if self.memories_dir.is_absolute() else (root / self.memories_dir)


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: EmbeddingProviderKind = "auto"
    model: str = "BAAI/bge-small-en-v1.5"
    dimension: int = Field(default=384, ge=8, le=4096)
    cache_dir: Path | None = Path("data/fastembed_cache")
    batch_size: int = Field(default=32, ge=1, le=1024)
    allow_download: bool = False

    def resolve_cache_dir(self, root: Path) -> Path | None:
        if env_cache := os.getenv("FASTEMBED_CACHE_PATH"):
            return Path(env_cache)
        if self.cache_dir is None:
            return None
        return self.cache_dir if self.cache_dir.is_absolute() else (root / self.cache_dir)


class DistillerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: DistillerProviderKind = "heuristic"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    auto_distill_on_scan: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0.0)

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @property
    def zero_cost_mode(self) -> bool:
        return self.provider == "heuristic" or (
            self.provider == "ollama" and not self.api_key_configured
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return API-safe distiller settings without exposing any API key."""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": redact_url(self.base_url) if self.base_url else None,
            "api_key_configured": self.api_key_configured,
            "auto_distill_on_scan": self.auto_distill_on_scan,
            "zero_cost_mode": self.zero_cost_mode,
        }


class AdaptersConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    watch_enabled: bool = True
    watch_debounce_ms: int = Field(default=2000, ge=50)
    scan_on_startup: bool = True
    enabled: list[str] = Field(default_factory=lambda: list(DEFAULT_ENABLED_ADAPTERS))


class RedactionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class OmnimexConfig(BaseModel):
    """Top-level configuration for Omnimex."""

    model_config = ConfigDict(extra="ignore")

    root_dir: Path = Field(default_factory=Path.cwd)
    app: AppConfig = Field(default_factory=AppConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    distiller: DistillerConfig = Field(default_factory=DistillerConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @property
    def db_path(self) -> Path:
        return self.storage.resolve_db_path(self.root_dir)

    @property
    def memories_dir(self) -> Path:
        return self.storage.resolve_memories_dir(self.root_dir)

    @property
    def embedding_cache_dir(self) -> Path | None:
        return self.embedding.resolve_cache_dir(self.root_dir)

    def to_public_dict(self) -> dict[str, Any]:
        """Return a redacted dictionary view safe for diagnostics/API responses."""
        raw = self.model_dump(mode="json")
        raw["distiller"] = self.distiller.to_public_dict()
        return redact_config(raw)


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Locate omnimex.toml from env var, start_dir, or parent directories."""
    if env_path := os.getenv("OMNIMEX_CONFIG"):
        candidate = Path(env_path).expanduser().resolve()
        if candidate.is_file():
            return candidate

    search_root = (start_dir or Path.cwd()).resolve()
    for directory in (search_root, *search_root.parents):
        candidate = directory / "omnimex.toml"
        if candidate.is_file():
            return candidate
    return None


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    storage = dict(data.get("storage", {}))
    embedding = dict(data.get("embedding", {}))
    distiller = dict(data.get("distiller", {}))

    if val := os.getenv("OMNIMEX_DB_PATH"):
        storage["db_path"] = val
    if val := os.getenv("OMNIMEX_MEMORIES_DIR"):
        storage["memories_dir"] = val

    if val := os.getenv("OMNIMEX_EMBEDDING_PROVIDER"):
        embedding["provider"] = val
    if val := os.getenv("OMNIMEX_EMBEDDING_MODEL"):
        embedding["model"] = val
    if (allow_dl := _parse_bool_env("OMNIMEX_ALLOW_MODEL_DOWNLOAD")) is not None:
        embedding["allow_download"] = allow_dl

    if val := os.getenv("OMNIMEX_DISTILLER_PROVIDER"):
        distiller["provider"] = val
    if val := os.getenv("OMNIMEX_DISTILLER_MODEL"):
        distiller["model"] = val
    if val := os.getenv("OMNIMEX_DISTILLER_BASE_URL"):
        distiller["base_url"] = val
    if val := os.getenv("OMNIMEX_DISTILLER_API_KEY"):
        distiller["api_key"] = val

    data["storage"] = storage
    data["embedding"] = embedding
    data["distiller"] = distiller
    return data


def load_config(
    config_path: str | Path | None = None,
    *,
    root_dir: str | Path | None = None,
) -> OmnimexConfig:
    """Load OmnimexConfig from omnimex.toml, optional omnimex.local.toml, and env vars."""
    resolved_file: Path | None = None
    if config_path is not None:
        resolved_file = Path(config_path).expanduser().resolve()
    else:
        resolved_file = find_config_file(Path(root_dir) if root_dir else None)

    resolved_root = (
        Path(root_dir).expanduser().resolve()
        if root_dir is not None
        else (resolved_file.parent if resolved_file is not None else Path.cwd().resolve())
    )

    merged: dict[str, Any] = {}
    if resolved_file is not None and resolved_file.is_file():
        with resolved_file.open("rb") as fh:
            merged = tomllib.load(fh)

    local_override = resolved_root / "omnimex.local.toml"
    if local_override.is_file():
        with local_override.open("rb") as fh:
            merged = _deep_merge(merged, tomllib.load(fh))

    merged = _apply_env_overrides(merged)
    merged["root_dir"] = resolved_root
    return OmnimexConfig.model_validate(merged)


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_local_distiller_config(
    distiller: DistillerConfig,
    *,
    root_dir: str | Path | None = None,
) -> Path:
    """Persist distiller overrides to omnimex.local.toml (gitignored)."""
    target_root = Path(root_dir).expanduser().resolve() if root_dir else Path.cwd().resolve()
    local_path = target_root / "omnimex.local.toml"

    existing: dict[str, Any] = {}
    if local_path.is_file():
        with local_path.open("rb") as fh:
            existing = tomllib.load(fh)

    distiller_section: dict[str, Any] = {
        "provider": distiller.provider,
        "auto_distill_on_scan": distiller.auto_distill_on_scan,
        "timeout_seconds": distiller.timeout_seconds,
    }
    if distiller.model is not None:
        distiller_section["model"] = distiller.model
    if distiller.base_url is not None:
        distiller_section["base_url"] = distiller.base_url
    if distiller.api_key is not None:
        distiller_section["api_key"] = distiller.api_key

    existing["distiller"] = distiller_section

    lines: list[str] = []
    for section, content in existing.items():
        if isinstance(content, dict):
            lines.append(f"[{section}]")
            for k, v in content.items():
                if v is not None:
                    lines.append(f"{k} = {_format_toml_value(v)}")
            lines.append("")
        elif content is not None:
            lines.append(f"{section} = {_format_toml_value(content)}")

    local_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return local_path


__all__ = [
    "AdaptersConfig",
    "AppConfig",
    "DEFAULT_ENABLED_ADAPTERS",
    "DistillerConfig",
    "DistillerProviderKind",
    "EmbeddingConfig",
    "EmbeddingProviderKind",
    "OmnimexConfig",
    "RedactionConfig",
    "StorageConfig",
    "find_config_file",
    "load_config",
    "save_local_distiller_config",
]
