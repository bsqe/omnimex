"""Tests for Omnimex core models, config, redaction, SQLite WAL+FTS5 DB, embeddings, and RRF search."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from omnimex.core.config import (
    DistillerConfig,
    EmbeddingConfig,
    OmnimexConfig,
    load_config,
    save_local_distiller_config,
)
from omnimex.core.models import (
    HandoffBrief,
    HandoffMemoryRef,
    HandoffSections,
    IngestCursor,
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    SearchHit,
    ToolCallRecord,
    normalize_workspace_path,
)
from omnimex.core.redaction import (
    contains_secrets,
    redact_config,
    redact_handoff_brief,
    redact_memory_card,
    redact_session,
    redact_text,
    redact_turn,
    redact_url,
)
from omnimex.storage.db import OmnimexDB
from omnimex.storage.embeddings import (
    FastEmbedLocalEmbedder,
    OfflineHashEmbedder,
    cosine_similarities,
    cosine_similarity,
    create_embedder,
    pack_embedding,
    unpack_embedding,
)
from omnimex.storage.search import HybridSearcher


def _make_sample_session(
    *,
    agent: str = "antigravity",
    external_id: str = "sess-001",
    workspace: str = r"E:\projects\omnimex",
    title: str = "Implement SQLite WAL and FTS5 search",
    turns: list[NormalizedTurn] | None = None,
) -> NormalizedSession:
    t0 = datetime(2026, 11, 17, 7, 40, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 17, 7, 45, 0, tzinfo=UTC)
    default_turns = turns or [
        NormalizedTurn(
            turn_index=0,
            role="user",
            content="Configure SQLite WAL mode with FTS5 and RRF hybrid search.",
            timestamp=t0,
        ),
        NormalizedTurn(
            turn_index=1,
            role="assistant",
            content="Enabled PRAGMA journal_mode=WAL and created turns_fts virtual table.",
            thinking="Check busy_timeout and foreign_keys pragma order.",
            tool_calls=[
                ToolCallRecord(
                    id="tc_1",
                    name="run_command",
                    arguments={"CommandLine": "pytest tests/test_core_storage.py"},
                    output="12 passed in 0.42s",
                    status="ok",
                    duration_ms=420,
                )
            ],
            timestamp=t1,
            model="claude-sonnet-4-5",
            token_count=180,
        ),
    ]
    return NormalizedSession(
        agent=agent,
        external_id=external_id,
        title=title,
        workspace=workspace,
        source_path=f"~/.gemini/{agent}/transcript.jsonl",
        started_at=t0,
        updated_at=t1,
        tags=["sqlite", "fts5"],
        turns=default_turns,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_models_normalization_and_defaults() -> None:
    assert normalize_workspace_path(r"E:\AAAAA project test\omnimex\\") == "E:/AAAAA project test/omnimex"
    assert normalize_workspace_path("E:\\") == "E:/"
    assert normalize_workspace_path("   ") is None

    session = _make_sample_session(
        turns=[
            NormalizedTurn(turn_index=0, role="human", content="Hi"),  # type: ignore[arg-type]
            NormalizedTurn(turn_index=1, role="model", content="Hello"),  # type: ignore[arg-type]
        ]
    )
    assert session.id == "antigravity:sess-001"
    assert session.workspace == "E:/projects/omnimex"
    assert session.turn_count == 2
    assert session.turns[0].role == "user"
    assert session.turns[1].role == "assistant"
    assert session.turns[0].id == "antigravity:sess-001#t0"
    assert session.turns[1].session_id == "antigravity:sess-001"

    card = MemoryCard(
        title="Use WAL mode",
        category="decision",
        content="Always enable WAL on Windows SQLite databases.",
        distill_status="heuristic",
        origin="distilled",
        workspace=r"E:\projects\omnimex",
    )
    assert card.id.startswith("mem_")
    assert card.workspace == "E:/projects/omnimex"
    assert card.created_at.tzinfo == UTC

    hit = SearchHit(
        turn_id="antigravity:sess-001#t1",
        session_id="antigravity:sess-001",
        agent="antigravity",
        turn_index=1,
        role="assistant",
        snippet="Enabled WAL",
        content="Enabled PRAGMA journal_mode=WAL",
        score=0.91,
        match_mode="hybrid",
    )
    assert hit.score == pytest.approx(0.91)

    brief = HandoffBrief(
        session_id=session.id,
        source_agent="antigravity",
        target_agent="claude_code",
        title="Handoff",
        markdown="# Brief",
        sections=HandoffSections(
            goal="Finish storage",
            relevant_memories=[HandoffMemoryRef(id=card.id, title=card.title, category="decision")],
        ),
        token_estimate=120,
    )
    assert brief.sections.relevant_memories[0].category == "decision"


# ---------------------------------------------------------------------------
# Config & omnimex.toml
# ---------------------------------------------------------------------------


def test_load_repo_config_and_local_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(repo_root / "omnimex.toml", root_dir=repo_root)
    assert isinstance(cfg, OmnimexConfig)
    assert cfg.db_path == repo_root / "data" / "omnimex.db"
    assert cfg.memories_dir == repo_root / "data" / "memories"
    assert cfg.embedding.provider == "auto"
    assert cfg.embedding.allow_download is False
    assert cfg.distiller.provider == "heuristic"
    assert cfg.distiller.zero_cost_mode is True
    assert cfg.distiller.api_key_configured is False

    # Test local TOML persistence and secret scrubbing in public view
    (tmp_path / "omnimex.toml").write_text(
        (repo_root / "omnimex.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    save_local_distiller_config(
        DistillerConfig(
            provider="openai_compatible",
            model="gpt-4o-mini",
            base_url="https://user:secretpass@api.example.com/v1?api_key=rawkey",
            api_key="sk-live-secret-1234567890abcdef",
            auto_distill_on_scan=False,
        ),
        root_dir=tmp_path,
    )
    loaded = load_config(root_dir=tmp_path)
    assert loaded.distiller.provider == "openai_compatible"
    assert loaded.distiller.api_key_configured is True
    assert loaded.distiller.zero_cost_mode is False
    public = loaded.to_public_dict()
    assert "api_key" not in public["distiller"]
    assert public["distiller"]["api_key_configured"] is True
    assert "secretpass" not in str(public["distiller"]["base_url"])
    assert "rawkey" not in str(public["distiller"]["base_url"])

    # Env override check
    monkeypatch.setenv("OMNIMEX_DISTILLER_PROVIDER", "ollama")
    monkeypatch.setenv("OMNIMEX_ALLOW_MODEL_DOWNLOAD", "true")
    env_cfg = load_config(root_dir=tmp_path)
    assert env_cfg.distiller.provider == "ollama"
    assert env_cfg.embedding.allow_download is True


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_scrubs_all_secret_types_and_preserves_safe_text() -> None:
    sample_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    sample_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAtestkeymaterial123456\n"
        "-----END RSA PRIVATE KEY-----"
    )
    raw = (
        "OpenAI sk-proj-abcd1234efgh5678ijkl9012\n"
        "Anthropic sk-ant-api03-abcdef1234567890\n"
        "GitHub ghp_1234567890abcdef1234567890abcdef1234\n"
        "Google AIzaSyDummyKeyValue1234567890abcd\n"
        "Header Authorization: Bearer supersecret_token_987654321\n"
        f"JWT {sample_jwt}\n"
        "DB postgresql://admin:mySecretPass@db.internal:5432/prod?sslpassword=pgpass&sslmode=require\n"
        f"{sample_pem}\n"
        "Safe note: using scikit-learn and sk-learn docs on E:/projects/omnimex."
    )
    assert contains_secrets(raw) is True
    scrubbed = redact_text(raw)

    for leaked in (
        "sk-proj-abcd1234efgh5678ijkl9012",
        "sk-ant-api03-abcdef1234567890",
        "ghp_1234567890abcdef1234567890abcdef1234",
        "AIzaSyDummyKeyValue1234567890abcd",
        "supersecret_token_987654321",
        sample_jwt,
        "mySecretPass",
        "pgpass",
        "MIIEowIBAAKCAQEAtestkeymaterial123456",
    ):
        assert leaked not in scrubbed

    assert "postgresql://***@db.internal:5432/prod?" in scrubbed
    assert "sslmode=require" in scrubbed
    assert "scikit-learn" in scrubbed
    assert "sk-learn" in scrubbed
    # Idempotency
    assert redact_text(scrubbed) == scrubbed

    # URL and config dict redaction
    assert redact_url("redis://default:topsecret@127.0.0.1:6379/0") == "redis://***@127.0.0.1:6379/0"
    cfg_redacted = redact_config(
        {
            "api_key": "sk-1234567890abcdef",
            "database_url": "postgresql://u:p@localhost/db",
            "provider": "heuristic",
        }
    )
    assert "api_key" not in cfg_redacted
    assert cfg_redacted["database_url"] == "postgresql://***@localhost/db"
    assert cfg_redacted["provider"] == "heuristic"

    # Turn, session, memory card, handoff brief redaction
    turn = NormalizedTurn(
        turn_index=0,
        role="assistant",
        content="Use key sk-testsecret1234567890",
        thinking="Token ghp_abcdef1234567890abcdef1234567890",
        tool_calls=[
            ToolCallRecord(
                name="http",
                arguments={
                    "token": "raw_token_12345678",
                    "signing_key": "raw_signing_key_123",
                    "primary_key": "id",
                    "sort_key": "created_at",
                    "foreign_key": "session_id",
                    "partition_key": "tenant_a",
                    "key_name": "main",
                    "hotkey": "ctrl+k",
                    "url": "https://u:pw@host/api",
                },
                output="Authorization: Bearer xyz9876543210abcdef",
            )
        ],
    )
    clean_turn = redact_turn(turn)
    assert clean_turn.redacted is True
    assert "sk-testsecret1234567890" not in clean_turn.content
    assert "ghp_abcdef" not in (clean_turn.thinking or "")
    assert clean_turn.tool_calls[0].arguments["token"] == "[REDACTED_SECRET]"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["signing_key"] == "[REDACTED_SECRET]"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["primary_key"] == "id"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["sort_key"] == "created_at"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["foreign_key"] == "session_id"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["partition_key"] == "tenant_a"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["key_name"] == "main"  # type: ignore[index]
    assert clean_turn.tool_calls[0].arguments["hotkey"] == "ctrl+k"  # type: ignore[index]
    assert "pw" not in str(clean_turn.tool_calls[0].arguments["url"])  # type: ignore[index]
    assert "xyz9876543210abcdef" not in (clean_turn.tool_calls[0].output or "")

    clean_session = redact_session(_make_sample_session(turns=[turn]))
    assert clean_session.turns[0].redacted is True

    clean_card = redact_memory_card(
        MemoryCard(
            title="Key AIzaSyDummyKeyValue1234567890abcd",
            category="pitfall",
            content="Never commit sk-testsecret1234567890",
        )
    )
    assert "AIzaSy" not in clean_card.title
    assert "sk-testsecret" not in clean_card.content

    clean_brief = redact_handoff_brief(
        HandoffBrief(
            session_id="s1",
            source_agent="codex",
            target_agent="claude_code",
            title="Handoff",
            markdown="DB mysql://root:pw123@localhost/app",
            sections=HandoffSections(goal="Rotate ghp_1234567890abcdef1234567890abcdef1234"),
        )
    )
    assert "pw123" not in clean_brief.markdown
    assert "ghp_123456" not in clean_brief.sections.goal


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def test_offline_embedder_fallback_and_fastembed_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = EmbeddingConfig(
        provider="auto",
        cache_dir=tmp_path / "empty_cache",
        allow_download=False,
    )
    embedder = create_embedder(cfg, root_dir=tmp_path)
    assert isinstance(embedder, OfflineHashEmbedder)
    assert embedder.is_fallback is True
    assert embedder.dimension == 384

    v1 = embedder.embed_query("sqlite wal fts5 hybrid search")
    v2 = embedder.embed_query("sqlite wal fts5 hybrid search")
    v3 = embedder.embed_query("sqlite wal mode locking")
    v4 = embedder.embed_query("react tailwind frontend button css")

    assert v1 == pytest.approx(v2)
    assert np.linalg.norm(v1) == pytest.approx(1.0, abs=1e-5)
    assert cosine_similarity(v1, v3) > cosine_similarity(v1, v4)

    # Pack/unpack float32 roundtrip + batch cosine similarities
    blob = pack_embedding(v1)
    assert len(blob) == 384 * 4
    unpacked = unpack_embedding(blob)
    assert np.allclose(unpacked, np.asarray(v1, dtype=np.float32))
    sims = cosine_similarities(unpacked, np.vstack([v3, v4]).astype(np.float32))
    assert sims[0] > sims[1]

    # Mocked FastEmbed path verifies local ONNX wrapper without hitting network
    class FakeTextEmbedding:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def embed(self, texts: list[str], batch_size: int = 32):  # noqa: ARG002
            for _ in texts:
                yield np.ones(384, dtype=np.float32)

    import fastembed

    cache_dir = tmp_path / "cached_fastembed"
    model_sub = cache_dir / "models--Qdrant--bge-small-en-v1.5-onnx-Q" / "snapshots" / "rev1"
    model_sub.mkdir(parents=True)
    (model_sub / "model_optimized.onnx").write_bytes(b"fake-onnx")

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    fe_embedder = FastEmbedLocalEmbedder(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir=cache_dir,
        allow_download=False,
    )
    assert fe_embedder.is_fallback is False
    assert fe_embedder.dimension == 384
    fe_vec = fe_embedder.embed_query("hello")
    assert np.linalg.norm(fe_vec) == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# SQLite WAL + FTS5 + Cursors + Memories + Hybrid Search
# ---------------------------------------------------------------------------


def test_db_wal_cursors_memories_and_redacted_storage(tmp_path: Path) -> None:
    db_file = tmp_path / "omnimex.db"
    with OmnimexDB(db_file) as db:
        assert db.journal_mode() == "wal"

        # Secret inside turn must be redacted before hitting SQLite & FTS5
        secret_session = _make_sample_session(
            external_id="secret-sess",
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="user",
                    content="My API key is sk-proj-verysecret1234567890abcd for testing.",
                    timestamp=datetime(2026, 11, 17, 8, 0, 0, tzinfo=UTC),
                )
            ],
        )
        db.upsert_session(secret_session)
        fetched = db.get_session("antigravity:secret-sess")
        assert fetched is not None
        assert fetched.turns[0].redacted is True
        assert "sk-proj-verysecret1234567890abcd" not in fetched.turns[0].content

        raw_fts = db.connection.execute(
            "SELECT COUNT(*) FROM turns_fts WHERE turns_fts MATCH '\"verysecret1234567890abcd\"'"
        ).fetchone()[0]
        assert raw_fts == 0

        # Incremental turn append
        db.upsert_turns(
            "antigravity:secret-sess",
            [
                NormalizedTurn(
                    turn_index=1,
                    role="assistant",
                    content="Scrubbed secret cleanly and enabled WAL mode.",
                    timestamp=datetime(2026, 11, 17, 8, 1, 0, tzinfo=UTC),
                )
            ],
        )
        updated = db.get_session("antigravity:secret-sess")
        assert updated is not None
        assert updated.turn_count == 2
        assert len(updated.turns) == 2

        # Cursor CRUD with Windows slash normalization
        cursor = IngestCursor(
            source_path=r"C:\Users\haha\.claude\history.jsonl",
            agent="claude_code",
            byte_offset=1024,
            line_number=42,
            mtime_ns=999999,
            file_size=2048,
            checksum="abc123",
        )
        db.upsert_cursor(cursor)
        loaded_cursor = db.get_cursor("C:/Users/haha/.claude/history.jsonl")
        assert loaded_cursor is not None
        assert loaded_cursor.byte_offset == 1024
        assert loaded_cursor.line_number == 42
        assert len(db.list_cursors("claude_code")) == 1
        assert db.delete_cursor(r"C:\Users\haha\.claude\history.jsonl") is True

        # Memories CRUD + stats
        card = MemoryCard(
            id="mem_wal_1",
            title="Enable WAL on Windows",
            category="pitfall",
            content="Always enable WAL and busy_timeout=5000.",
            workspace=r"E:\projects\omnimex",
            distill_status="heuristic",
            origin="distilled",
            source_session_id="antigravity:secret-sess",
            source_agent="antigravity",
        )
        db.upsert_memory(card)
        assert db.get_memory("mem_wal_1") is not None
        assert len(db.list_memories(category="pitfall", workspace="E:/projects/omnimex")) == 1
        stats = db.get_stats()
        assert stats["total_sessions"] == 1
        assert stats["total_turns"] == 2
        assert stats["total_memories"] == 1
        assert stats["memories_by_category"]["pitfall"] == 1
        assert stats["memories_by_status"]["heuristic"] == 1

        # Cascade delete removes turns and embeddings
        embedder = OfflineHashEmbedder()
        assert db.index_session_embeddings("antigravity:secret-sess", embedder) == 2
        assert db.count_turn_embeddings(embedder.model_name) == 2
        assert db.delete_session("antigravity:secret-sess") is True
        assert db.count_turn_embeddings() == 0
        assert db.delete_memory("mem_wal_1") is True


def test_hybrid_rrf_fts_and_vector_search(tmp_path: Path) -> None:
    db_file = tmp_path / "search.db"
    embedder = OfflineHashEmbedder()

    with OmnimexDB(db_file) as db:
        s1 = _make_sample_session(
            agent="antigravity",
            external_id="s1",
            workspace=r"E:\projects\omnimex",
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="assistant",
                    content="Configured SQLite WAL journal mode with FTS5 BM25 ranking and RRF fusion.",
                    timestamp=datetime(2026, 11, 17, 6, 0, 0, tzinfo=UTC),
                ),
                NormalizedTurn(
                    turn_index=1,
                    role="user",
                    content="Let us adjust the Tailwind navbar padding on the React frontend.",
                    timestamp=datetime(2026, 11, 17, 6, 5, 0, tzinfo=UTC),
                ),
            ],
        )
        s2 = _make_sample_session(
            agent="claude_code",
            external_id="s2",
            workspace=r"E:\projects\other-app",
            turns=[
                NormalizedTurn(
                    turn_index=0,
                    role="assistant",
                    content="Vector similarity search over float32 numpy blobs with SQLite WAL.",
                    timestamp=datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC),
                )
            ],
        )
        db.upsert_session(s1)
        db.upsert_session(s2)
        db.index_session_embeddings("antigravity:s1", embedder)
        db.index_session_embeddings("claude_code:s2", embedder)

        searcher = HybridSearcher(db, embedder)

        # Hybrid search ranks the SQLite WAL + FTS5 + RRF turn #1
        hybrid_page = searcher.search_page("SQLite WAL FTS5 RRF", mode="hybrid", limit=10)
        assert hybrid_page.total >= 2
        assert hybrid_page.hits[0].turn_id == "antigravity:s1#t0"
        assert hybrid_page.hits[0].match_mode == "hybrid"
        assert hybrid_page.hits[0].fts_score is not None
        assert hybrid_page.hits[0].vector_score is not None
        assert 0.0 < hybrid_page.hits[0].score <= 1.0

        # FTS-only mode handles punctuation & Windows paths safely without FTS5 syntax error
        fts_hits = searcher.search(r"SQLite (WAL): C:\omnimex\db.py", mode="fts")
        assert len(fts_hits) >= 1
        assert fts_hits[0].match_mode == "fts"

        # Vector-only mode
        vec_hits = searcher.search("float32 numpy vector cosine", mode="vector")
        assert len(vec_hits) >= 1
        assert vec_hits[0].turn_id == "claude_code:s2#t0"
        assert vec_hits[0].match_mode == "vector"

        # Workspace & agent & date filters
        filtered = searcher.search_page(
            "SQLite WAL",
            agents=["claude_code"],
            workspace="E:/projects/other-app",
            date_from="2026-11-16",
            date_to="2026-11-16",
            mode="hybrid",
        )
        assert filtered.total == 1
        assert filtered.hits[0].session_id == "claude_code:s2"
        assert filtered.facets["agents"] == {"claude_code": 1}

        # Empty query browse returns filtered recent turns
        recent = searcher.search("", agents=["antigravity"], limit=5)
        assert len(recent) == 2
        assert recent[0].turn_id == "antigravity:s1#t1"

        # Pagination + hybrid degradation when embedder=None
        fts_only_hybrid = HybridSearcher(db, None).search_page("SQLite WAL", mode="hybrid", limit=1, offset=1)
        assert fts_only_hybrid.total >= 2
        assert len(fts_only_hybrid.hits) == 1


def _run_immediate_coro(coro: Any) -> Any:
    """Drive an immediate (non-suspending) coroutine without opening asyncio sockets."""
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise AssertionError("Coroutine unexpectedly suspended")


def test_async_embedder_and_extra_redaction_and_subpath_filters(tmp_path: Path) -> None:
    embedder = OfflineHashEmbedder()
    vecs = _run_immediate_coro(embedder.aembed_texts(["alpha beta", "gamma delta"]))
    qvec = _run_immediate_coro(embedder.aembed_query("alpha beta"))
    assert len(vecs) == 2
    assert cosine_similarity(vecs[0], qvec) == pytest.approx(1.0, abs=1e-5)

    # Extra secret formats: AWS, Slack, HF, unclosed PEM, .env assignment, scheme-less Redis URL
    raw = (
        "AWS=AKIAIOSFODNN7EXAMPLE\n"
        "SLACK=xoxb-1234567890-abcdef123456\n"
        "HF=hf_abcdefghijklmnopqrstuvwxyz1234\n"
        "OPENAI_API_KEY='sk-test-env-secret-99999999'\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    )
    cleaned = redact_text(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in cleaned
    assert "xoxb-1234567890-abcdef123456" not in cleaned
    assert "hf_abcdefghijklmnopqrstuvwxyz1234" not in cleaned
    assert "sk-test-env-secret-99999999" not in cleaned
    assert "OPENSSH PRIVATE KEY" not in cleaned
    assert redact_url("default:secretpass@127.0.0.1:6379?token=tok123") == "***@127.0.0.1:6379?token=***"

    # Workspace subpath filtering & replace_turns count update
    with OmnimexDB(tmp_path / "subpath.db") as db:
        sess = _make_sample_session(external_id="sub1", workspace=r"E:\projects\omnimex\frontend")
        db.upsert_session(sess)
        sessions, total = db.list_sessions(workspace=r"E:\projects\omnimex")
        assert total == 1
        assert sessions[0].id == "antigravity:sub1"

        # Replace turns with 1 turn updates turn_count down to 1
        smaller = sess.model_copy(
            update={
                "turn_count": 0,
                "turns": [NormalizedTurn(turn_index=0, role="user", content="Only one turn")],
            }
        )
        updated = db.upsert_session(smaller, replace_turns=True)
        assert updated.turn_count == 1
        fetched = db.get_session("antigravity:sub1")
        assert fetched is not None
        assert fetched.turn_count == 1
        assert len(fetched.turns) == 1
