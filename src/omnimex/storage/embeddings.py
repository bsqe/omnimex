"""Local ONNX FastEmbed embedder with automatic deterministic offline fallback."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from omnimex.core.config import EmbeddingConfig

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def pack_embedding(vector: Sequence[float] | np.ndarray) -> bytes:
    """Serialize a float vector into compact little-endian float32 bytes."""
    arr = np.asarray(vector, dtype=np.float32)
    return arr.tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    """Deserialize float32 bytes into a 1-D numpy float32 array."""
    return np.frombuffer(blob, dtype=np.float32)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    """Return L2-normalized float32 vector (zero vector stays zero)."""
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        return np.zeros_like(arr, dtype=np.float32)
    return (arr / norm).astype(np.float32)


def cosine_similarity(vec_a: Sequence[float] | np.ndarray, vec_b: Sequence[float] | np.ndarray) -> float:
    """Compute cosine similarity between two 1-D vectors."""
    a = np.asarray(vec_a, dtype=np.float32)
    b = np.asarray(vec_b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


def cosine_similarities(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Compute vectorized cosine similarity between query_vec (D,) and matrix (N, D)."""
    if matrix.size == 0:
        return np.empty(0, dtype=np.float32)
    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm <= 1e-12:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    m_norms = np.linalg.norm(matrix, axis=1)
    safe_norms = np.where(m_norms > 1e-12, m_norms * q_norm, 1.0)
    dots = matrix @ q
    sims = np.where(m_norms > 1e-12, dots / safe_norms, 0.0)
    return sims.astype(np.float32)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol implemented by all embedding providers."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def is_fallback(self) -> bool: ...

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    async def aembed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def aembed_query(self, text: str) -> list[float]: ...


class OfflineHashEmbedder:
    """Deterministic zero-network feature-hashing embedder (signed n-grams).

    Produces L2-normalized vectors in ``dimension`` dims using BLAKE2b so vectors
    are deterministic across processes and platforms without downloading models.
    """

    def __init__(
        self,
        *,
        dimension: int = 384,
        model_name: str = "offline-hash-v1",
    ) -> None:
        self._dimension = dimension
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_fallback(self) -> bool:
        return True

    def _encode_one(self, text: str) -> list[float]:
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return [0.0] * self._dimension

        vec = np.zeros(self._dimension, dtype=np.float32)
        features: list[tuple[str, float]] = []

        for idx, tok in enumerate(tokens):
            features.append((f"w:{tok}", 1.0))
            if idx + 1 < len(tokens):
                features.append((f"b:{tok}_{tokens[idx + 1]}", 1.25))
            if len(tok) >= 4:
                padded = f"^{tok}$"
                for n in (3, 4):
                    for pos in range(len(padded) - n + 1):
                        features.append((f"c{n}:{padded[pos : pos + n]}", 0.35))

        for feat, weight in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            val = int.from_bytes(digest, "little", signed=False)
            bucket = val % self._dimension
            sign = 1.0 if ((val >> 32) & 1) else -1.0
            vec[bucket] += sign * weight

        normalized = l2_normalize(vec)
        return normalized.tolist()

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._encode_one(text)

    async def aembed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_texts(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return self.embed_query(text)


def _has_cached_onnx_model(model_name: str, cache_dir: Path | None) -> bool:
    """Return True if a FastEmbed ONNX artifact is already cached locally."""
    search_roots: list[Path] = []
    if cache_dir is not None:
        search_roots.append(cache_dir)
    elif env_cache := os.getenv("FASTEMBED_CACHE_PATH"):
        search_roots.append(Path(env_cache))
    else:
        search_roots.append(Path(tempfile.gettempdir()) / "fastembed_cache")

    candidates: list[str] = []
    try:
        from fastembed import TextEmbedding

        for desc in TextEmbedding._list_supported_models():
            if desc.model.lower() == model_name.lower() and desc.sources.hf:
                candidates.append(f"models--{desc.sources.hf.replace('/', '--')}")
    except Exception:
        pass

    for root in search_roots:
        if not root.is_dir():
            continue
        if candidates:
            for folder_name in candidates:
                target = root / folder_name
                if target.is_dir() and any(target.rglob("*.onnx")):
                    return True
        else:
            # Fallback heuristic if FastEmbed metadata changed
            slug = model_name.split("/")[-1].lower()
            for child in root.iterdir():
                if child.is_dir() and slug in child.name.lower() and any(child.rglob("*.onnx")):
                    return True
    return False


class FastEmbedLocalEmbedder:
    """Local ONNX embedder backed by ``fastembed.TextEmbedding``."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        *,
        cache_dir: Path | None = None,
        batch_size: int = 32,
        allow_download: bool = False,
        dimension: int = 384,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError("fastembed is not installed") from exc

        if not allow_download and not _has_cached_onnx_model(model_name, cache_dir):
            raise RuntimeError(
                f"FastEmbed model '{model_name}' is not cached locally and allow_download=False"
            )

        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "enable_cpu_mem_arena": False,
        }
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(cache_dir)

        if not allow_download:
            kwargs["local_files_only"] = True

        self._model = TextEmbedding(**kwargs)
        self._model_name = model_name
        self._batch_size = batch_size

        probe = list(self._model.embed(["dimension probe"], batch_size=1))
        self._dimension = len(probe[0]) if probe else dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_fallback(self) -> bool:
        return False

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = list(self._model.embed(list(texts), batch_size=self._batch_size))
        return [l2_normalize(np.asarray(v, dtype=np.float32)).tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        if hasattr(self._model, "query_embed"):
            vectors = list(self._model.query_embed(text))
            if vectors:
                return l2_normalize(np.asarray(vectors[0], dtype=np.float32)).tolist()
        return self.embed_texts([text])[0]

    async def aembed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed_texts, texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_query, text)


def create_embedder(
    config: EmbeddingConfig | None = None,
    *,
    root_dir: Path | None = None,
    allow_download: bool | None = None,
) -> EmbeddingProvider:
    """Create the configured embedding provider with automatic offline fallback.

    When ``config.provider == "auto"`` (the default), local ONNX ``fastembed``
    is used if cached locally (or downloadable when ``allow_download=True``),
    otherwise falls back deterministically to ``OfflineHashEmbedder``.
    """
    cfg = config or EmbeddingConfig()
    effective_allow_download = cfg.allow_download if allow_download is None else allow_download
    resolved_cache = cfg.resolve_cache_dir(root_dir or Path.cwd())

    if cfg.provider == "offline_hash":
        return OfflineHashEmbedder(dimension=cfg.dimension)

    if cfg.provider == "fastembed":
        return FastEmbedLocalEmbedder(
            model_name=cfg.model,
            cache_dir=resolved_cache,
            batch_size=cfg.batch_size,
            allow_download=effective_allow_download,
            dimension=cfg.dimension,
        )

    # Auto mode: prefer FastEmbed when cached or explicitly allowed to download,
    # never hang offline/test runs.
    try:
        return FastEmbedLocalEmbedder(
            model_name=cfg.model,
            cache_dir=resolved_cache,
            batch_size=cfg.batch_size,
            allow_download=effective_allow_download,
            dimension=cfg.dimension,
        )
    except Exception as exc:
        logger.debug(
            "Using deterministic OfflineHashEmbedder fallback (%s)",
            exc,
        )
        return OfflineHashEmbedder(dimension=cfg.dimension)


__all__ = [
    "EmbeddingProvider",
    "FastEmbedLocalEmbedder",
    "OfflineHashEmbedder",
    "cosine_similarities",
    "cosine_similarity",
    "create_embedder",
    "l2_normalize",
    "pack_embedding",
    "unpack_embedding",
]
