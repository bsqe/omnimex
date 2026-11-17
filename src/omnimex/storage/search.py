"""Hybrid search combining SQLite FTS5 BM25 and float32 vector cosine similarity via RRF."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from omnimex.core.models import SearchHit, SearchMode
from omnimex.storage.db import OmnimexDB, append_workspace_filter, normalize_date_bound
from omnimex.storage.embeddings import (
    EmbeddingProvider,
    cosine_similarities,
    unpack_embedding,
)

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class SearchResultPage:
    """Paginated search result with facets and timing."""

    query: str
    mode: SearchMode
    total: int
    took_ms: float
    hits: list[SearchHit] = field(default_factory=list)
    facets: dict[str, dict[str, int]] = field(default_factory=dict)


def tokenize_fts_query(raw_query: str) -> list[str]:
    """Extract safe alphanumeric/underscore tokens for FTS5 queries."""
    return _FTS_TOKEN_RE.findall(raw_query)


def build_fts_match_expressions(raw_query: str) -> list[str]:
    """Build sanitized FTS5 MATCH expressions (AND first, then OR fallback)."""
    tokens = tokenize_fts_query(raw_query)
    if not tokens:
        return []
    quoted = [f'"{tok}"' for tok in tokens]
    if len(quoted) == 1:
        return [quoted[0]]
    return [" AND ".join(quoted), " OR ".join(quoted)]


def _make_excerpt(text: str, tokens: Sequence[str], max_len: int = 240) -> str:
    """Return a readable excerpt centered near the first query token match."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned

    lower = cleaned.lower()
    hit_pos = -1
    for tok in tokens:
        pos = lower.find(tok.lower())
        if pos != -1 and (hit_pos == -1 or pos < hit_pos):
            hit_pos = pos

    if hit_pos == -1:
        return cleaned[: max_len - 3].rstrip() + "..."

    half = max_len // 3
    start = max(0, hit_pos - half)
    end = min(len(cleaned), start + max_len)
    if end - start < max_len:
        start = max(0, end - max_len)

    excerpt = cleaned[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(cleaned):
        excerpt = excerpt.rstrip() + "..."
    return excerpt


def _build_sql_filters(
    *,
    agents: Sequence[str] | None,
    workspace: str | None,
    date_from: datetime | str | None,
    date_to: datetime | str | None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if agents:
        placeholders = ",".join("?" for _ in agents)
        clauses.append(f"s.agent IN ({placeholders})")
        params.extend(agents)

    append_workspace_filter(clauses, params, "s.workspace_norm", workspace)

    if bound_from := normalize_date_bound(date_from, end_of_day=False):
        clauses.append("COALESCE(t.timestamp, s.updated_at) >= ?")
        params.append(bound_from)

    if bound_to := normalize_date_bound(date_to, end_of_day=True):
        clauses.append("COALESCE(t.timestamp, s.updated_at) <= ?")
        params.append(bound_to)

    return clauses, params


@dataclass
class _CandidateRow:
    turn_id: str
    session_id: str
    agent: str
    session_title: str
    workspace: str | None
    turn_index: int
    role: str
    content: str
    snippet: str
    timestamp: str | None
    fts_score: float | None = None
    vector_score: float | None = None
    raw_vector_sim: float | None = None


class HybridSearcher:
    """FTS5 BM25 + float32 vector cosine similarity + Reciprocal Rank Fusion."""

    def __init__(
        self,
        db: OmnimexDB,
        embedder: EmbeddingProvider | None = None,
        *,
        rrf_k: int = 60,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self._rrf_k = rrf_k

    def search(
        self,
        query: str = "",
        *,
        agents: Sequence[str] | None = None,
        workspace: str | None = None,
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        mode: SearchMode = "hybrid",
        limit: int = 20,
        offset: int = 0,
    ) -> list[SearchHit]:
        """Run search and return the matching ``SearchHit`` list."""
        return self.search_page(
            query,
            agents=agents,
            workspace=workspace,
            date_from=date_from,
            date_to=date_to,
            mode=mode,
            limit=limit,
            offset=offset,
        ).hits

    def search_page(
        self,
        query: str = "",
        *,
        agents: Sequence[str] | None = None,
        workspace: str | None = None,
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        mode: SearchMode = "hybrid",
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResultPage:
        """Run search and return paginated hits, total count, timing, and facets."""
        started = time.perf_counter()
        clean_query = query.strip()
        effective_limit = max(1, min(limit, 100))
        effective_offset = max(0, offset)

        if not clean_query:
            hits, total, facets = self._browse_recent(
                agents=agents,
                workspace=workspace,
                date_from=date_from,
                date_to=date_to,
                mode=mode,
                limit=effective_limit,
                offset=effective_offset,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return SearchResultPage(
                query=clean_query,
                mode=mode,
                total=total,
                took_ms=elapsed_ms,
                hits=hits,
                facets=facets,
            )

        candidate_depth = max((effective_limit + effective_offset) * 3, 60)

        fts_ranked: list[_CandidateRow] = []
        if mode in ("fts", "hybrid"):
            fts_ranked = self._fts_search(
                clean_query,
                agents=agents,
                workspace=workspace,
                date_from=date_from,
                date_to=date_to,
                limit=candidate_depth,
            )

        vec_ranked: list[_CandidateRow] = []
        if mode in ("vector", "hybrid") and self._embedder is not None:
            vec_ranked = self._vector_search(
                clean_query,
                agents=agents,
                workspace=workspace,
                date_from=date_from,
                date_to=date_to,
                limit=candidate_depth,
            )

        if mode == "fts" or (mode == "hybrid" and not vec_ranked and fts_ranked):
            all_hits = self._hits_from_single_list(fts_ranked, mode="fts" if mode == "fts" else "hybrid", use_fts=True)
        elif mode == "vector" or (mode == "hybrid" and not fts_ranked and vec_ranked):
            all_hits = self._hits_from_single_list(vec_ranked, mode="vector" if mode == "vector" else "hybrid", use_fts=False)
        else:
            all_hits = self._fuse_rrf(fts_ranked, vec_ranked)

        facets = self._compute_facets(all_hits)
        total = len(all_hits)
        paged = all_hits[effective_offset : effective_offset + effective_limit]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return SearchResultPage(
            query=clean_query,
            mode=mode,
            total=total,
            took_ms=elapsed_ms,
            hits=paged,
            facets=facets,
        )

    def _browse_recent(
        self,
        *,
        agents: Sequence[str] | None,
        workspace: str | None,
        date_from: datetime | str | None,
        date_to: datetime | str | None,
        mode: SearchMode,
        limit: int,
        offset: int,
    ) -> tuple[list[SearchHit], int, dict[str, dict[str, int]]]:
        clauses, params = _build_sql_filters(
            agents=agents,
            workspace=workspace,
            date_from=date_from,
            date_to=date_to,
        )
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._db.read_connection() as conn:
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM turns t
                JOIN sessions s ON s.id = t.session_id
                {where_sql}
                """,
                params,
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0

            rows = conn.execute(
                f"""
                SELECT t.id AS turn_id, t.session_id, s.agent, s.title AS session_title,
                       s.workspace, t.turn_index, t.role, t.content, t.search_text,
                       COALESCE(t.timestamp, s.updated_at) AS ts
                FROM turns t
                JOIN sessions s ON s.id = t.session_id
                {where_sql}
                ORDER BY COALESCE(t.timestamp, s.updated_at) DESC, t.rowid DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

        hits = [
            SearchHit(
                turn_id=r["turn_id"],
                session_id=r["session_id"],
                agent=r["agent"],
                session_title=r["session_title"] or "",
                workspace=r["workspace"],
                turn_index=int(r["turn_index"]),
                role=r["role"],
                snippet=_make_excerpt(r["content"] or r["search_text"] or "", ()),
                content=r["content"],
                timestamp=r["ts"],
                score=1.0,
                fts_score=None,
                vector_score=None,
                match_mode=mode,
            )
            for r in rows
        ]
        return hits, total, self._compute_facets(hits)

    def _fts_search(
        self,
        query: str,
        *,
        agents: Sequence[str] | None,
        workspace: str | None,
        date_from: datetime | str | None,
        date_to: datetime | str | None,
        limit: int,
    ) -> list[_CandidateRow]:
        expressions = build_fts_match_expressions(query)
        if not expressions:
            return []

        tokens = tokenize_fts_query(query)
        clauses, params = _build_sql_filters(
            agents=agents,
            workspace=workspace,
            date_from=date_from,
            date_to=date_to,
        )
        filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""

        rows: list[Any] = []
        with self._db.read_connection() as conn:
            for expr in expressions:
                rows = conn.execute(
                    f"""
                    SELECT t.id AS turn_id, t.session_id, s.agent, s.title AS session_title,
                           s.workspace, t.turn_index, t.role, t.content, t.search_text,
                           COALESCE(t.timestamp, s.updated_at) AS ts,
                           bm25(turns_fts) AS raw_bm25,
                           snippet(turns_fts, 0, '[', ']', '...', 24) AS fts_snippet
                    FROM turns_fts
                    JOIN turns t ON t.rowid = turns_fts.rowid
                    JOIN sessions s ON s.id = t.session_id
                    WHERE turns_fts MATCH ?
                    {filter_sql}
                    ORDER BY raw_bm25 ASC, t.rowid DESC
                    LIMIT ?
                    """,
                    [expr, *params, limit],
                ).fetchall()
                if rows:
                    break

        results: list[_CandidateRow] = []
        for r in rows:
            raw_bm25 = float(r["raw_bm25"])
            pos_bm25 = max(0.0, -raw_bm25)
            normalized_fts = round(pos_bm25 / (1.0 + pos_bm25), 6) if pos_bm25 > 0 else 0.01
            snippet_text = str(r["fts_snippet"] or "").strip()
            if not snippet_text:
                snippet_text = _make_excerpt(r["content"] or r["search_text"] or "", tokens)

            results.append(
                _CandidateRow(
                    turn_id=r["turn_id"],
                    session_id=r["session_id"],
                    agent=r["agent"],
                    session_title=r["session_title"] or "",
                    workspace=r["workspace"],
                    turn_index=int(r["turn_index"]),
                    role=r["role"],
                    content=r["content"],
                    snippet=snippet_text,
                    timestamp=r["ts"],
                    fts_score=normalized_fts,
                )
            )
        return results

    def _vector_search(
        self,
        query: str,
        *,
        agents: Sequence[str] | None,
        workspace: str | None,
        date_from: datetime | str | None,
        date_to: datetime | str | None,
        limit: int,
    ) -> list[_CandidateRow]:
        assert self._embedder is not None
        query_vec = np.asarray(self._embedder.embed_query(query), dtype=np.float32)
        if query_vec.size == 0 or float(np.linalg.norm(query_vec)) <= 1e-12:
            return []

        clauses, params = _build_sql_filters(
            agents=agents,
            workspace=workspace,
            date_from=date_from,
            date_to=date_to,
        )
        clauses.append("te.model = ?")
        clauses.append("te.dimension = ?")
        params.extend([self._embedder.model_name, int(query_vec.shape[0])])
        where_sql = f"WHERE {' AND '.join(clauses)}"

        with self._db.read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT t.id AS turn_id, t.session_id, s.agent, s.title AS session_title,
                       s.workspace, t.turn_index, t.role, t.content, t.search_text,
                       COALESCE(t.timestamp, s.updated_at) AS ts,
                       te.embedding
                FROM turn_embeddings te
                JOIN turns t ON t.id = te.turn_id
                JOIN sessions s ON s.id = t.session_id
                {where_sql}
                """,
                params,
            ).fetchall()

        if not rows:
            return []

        dim = int(query_vec.shape[0])
        vectors: list[np.ndarray] = []
        valid_rows: list[Any] = []
        for r in rows:
            arr = unpack_embedding(bytes(r["embedding"]))
            if arr.shape[0] == dim:
                vectors.append(arr)
                valid_rows.append(r)

        if not valid_rows:
            return []

        matrix = np.vstack(vectors)
        sims = cosine_similarities(query_vec, matrix)
        top_indices = np.argsort(-sims)[:limit]
        tokens = tokenize_fts_query(query)

        results: list[_CandidateRow] = []
        for idx in top_indices:
            sim = float(sims[int(idx)])
            r = valid_rows[int(idx)]
            clamped_score = round(max(0.0, min(1.0, sim)), 6)
            results.append(
                _CandidateRow(
                    turn_id=r["turn_id"],
                    session_id=r["session_id"],
                    agent=r["agent"],
                    session_title=r["session_title"] or "",
                    workspace=r["workspace"],
                    turn_index=int(r["turn_index"]),
                    role=r["role"],
                    content=r["content"],
                    snippet=_make_excerpt(r["content"] or r["search_text"] or "", tokens),
                    timestamp=r["ts"],
                    vector_score=clamped_score,
                    raw_vector_sim=sim,
                )
            )
        return results

    def _hits_from_single_list(
        self,
        items: Sequence[_CandidateRow],
        *,
        mode: SearchMode,
        use_fts: bool,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for item in items:
            score = item.fts_score if use_fts else item.vector_score
            hits.append(
                SearchHit(
                    turn_id=item.turn_id,
                    session_id=item.session_id,
                    agent=item.agent,
                    session_title=item.session_title,
                    workspace=item.workspace,
                    turn_index=item.turn_index,
                    role=item.role,  # type: ignore[arg-type]
                    snippet=item.snippet,
                    content=item.content,
                    timestamp=item.timestamp,
                    score=float(score or 0.0),
                    fts_score=item.fts_score,
                    vector_score=item.vector_score,
                    match_mode=mode,
                )
            )
        return hits

    def _fuse_rrf(
        self,
        fts_ranked: Sequence[_CandidateRow],
        vec_ranked: Sequence[_CandidateRow],
    ) -> list[SearchHit]:
        merged: dict[str, _CandidateRow] = {}
        fts_ranks: dict[str, int] = {}
        vec_ranks: dict[str, int] = {}

        for rank, item in enumerate(fts_ranked, start=1):
            fts_ranks[item.turn_id] = rank
            merged[item.turn_id] = item

        for rank, item in enumerate(vec_ranked, start=1):
            vec_ranks[item.turn_id] = rank
            if existing := merged.get(item.turn_id):
                existing.vector_score = item.vector_score
                existing.raw_vector_sim = item.raw_vector_sim
            else:
                merged[item.turn_id] = item

        active_lists = (1 if fts_ranked else 0) + (1 if vec_ranked else 0)
        max_rrf = active_lists / float(self._rrf_k + 1) if active_lists else 1.0

        scored: list[tuple[float, float, float, SearchHit]] = []
        for turn_id, item in merged.items():
            rrf = 0.0
            if turn_id in fts_ranks:
                rrf += 1.0 / float(self._rrf_k + fts_ranks[turn_id])
            if turn_id in vec_ranks:
                rrf += 1.0 / float(self._rrf_k + vec_ranks[turn_id])

            norm_score = round(min(1.0, rrf / max_rrf), 6)
            hit = SearchHit(
                turn_id=item.turn_id,
                session_id=item.session_id,
                agent=item.agent,
                session_title=item.session_title,
                workspace=item.workspace,
                turn_index=item.turn_index,
                role=item.role,  # type: ignore[arg-type]
                snippet=item.snippet,
                content=item.content,
                timestamp=item.timestamp,
                score=norm_score,
                fts_score=item.fts_score,
                vector_score=item.vector_score,
                match_mode="hybrid",
            )
            scored.append(
                (
                    norm_score,
                    item.fts_score or 0.0,
                    item.raw_vector_sim if item.raw_vector_sim is not None else -1.0,
                    hit,
                )
            )

        scored.sort(key=lambda tup: (tup[0], tup[1], tup[2]), reverse=True)
        return [tup[3] for tup in scored]

    @staticmethod
    def _compute_facets(hits: Sequence[SearchHit]) -> dict[str, dict[str, int]]:
        agents: dict[str, int] = {}
        workspaces: dict[str, int] = {}
        for h in hits:
            agents[h.agent] = agents.get(h.agent, 0) + 1
            if h.workspace:
                workspaces[h.workspace] = workspaces.get(h.workspace, 0) + 1
        return {"agents": agents, "workspaces": workspaces}


__all__ = [
    "HybridSearcher",
    "SearchResultPage",
    "build_fts_match_expressions",
    "tokenize_fts_query",
]
