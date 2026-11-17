"""SQLite WAL + FTS5 storage, local ONNX/offline embeddings, and RRF hybrid search."""

from omnimex.storage.db import OmnimexDB
from omnimex.storage.embeddings import (
    EmbeddingProvider,
    FastEmbedLocalEmbedder,
    OfflineHashEmbedder,
    cosine_similarities,
    cosine_similarity,
    create_embedder,
    pack_embedding,
    unpack_embedding,
)
from omnimex.storage.search import HybridSearcher, SearchResultPage

__all__ = [
    "EmbeddingProvider",
    "FastEmbedLocalEmbedder",
    "HybridSearcher",
    "OfflineHashEmbedder",
    "OmnimexDB",
    "SearchResultPage",
    "cosine_similarities",
    "cosine_similarity",
    "create_embedder",
    "pack_embedding",
    "unpack_embedding",
]
