"""Shared adapter primitives, safe Windows filesystem/SQLite helpers, and incremental cursors."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

import frontmatter
import yaml

from omnimex.core.models import (
    IngestCursor,
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    normalize_workspace_path,
)

logger = logging.getLogger(__name__)

HealthState = Literal["healthy", "degraded", "not_found", "permission_denied"]

_ENV_PATH_RE = re.compile(r"(?i)^file:///")
_DRIVE_LEADING_SLASH_RE = re.compile(r"^/([A-Za-z]:/)")


@dataclass(slots=True)
class AdapterSourceStatus:
    """Health and discovery summary for one agent adapter."""

    agent: str
    name: str
    status: HealthState
    source_paths: list[str] = field(default_factory=list)
    discovered_files: int = 0
    native_memory_files: int = 0
    errors: list[str] = field(default_factory=list)
    supports_native_memories: bool = False


@dataclass(slots=True)
class IncrementalParseResult:
    """Result returned by ``BaseAgentAdapter.parse_file_incremental``."""

    sessions: list[NormalizedSession]
    cursor: IngestCursor
    changed: bool = True
    is_append_only: bool = False


def normalize_source_path(path: str | Path) -> str:
    """Return a canonical forward-slash string path for storage and cursor keys."""
    return str(path).replace("\\", "/")


def build_native_memory_id(agent: str, source_path: str | Path) -> str:
    """Return a collision-free deterministic ID for a native agent memory path."""
    norm = normalize_source_path(source_path).lower()
    digest = hashlib.sha256(f"{agent}:{norm}".encode()).hexdigest()[:12]
    return f"native_{agent}_{digest}"


def safe_exists(path: Path) -> bool:
    """Check path existence while treating Windows WinError 5 / PermissionError as False."""
    try:
        return path.exists()
    except OSError:
        return False


def safe_is_file(path: Path) -> bool:
    """Return True if path is an accessible regular file."""
    try:
        return path.is_file()
    except OSError:
        return False


def safe_is_dir(path: Path, *, errors: list[str] | None = None) -> bool:
    """Return True if path is an accessible directory, recording PermissionError if given."""
    try:
        return path.is_dir()
    except PermissionError as exc:
        msg = f"permission_denied: {normalize_source_path(path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        return False
    except OSError:
        return False


def safe_stat(path: Path, *, errors: list[str] | None = None) -> os.stat_result | None:
    """Stat a file safely, recording PermissionError/OSError without raising."""
    try:
        return path.stat()
    except PermissionError as exc:
        msg = f"permission_denied: {normalize_source_path(path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        return None
    except OSError as exc:
        msg = f"stat_failed: {normalize_source_path(path)} ({exc})"
        logger.debug(msg)
        if errors is not None:
            errors.append(msg)
        return None


def safe_scandir(path: Path, *, errors: list[str] | None = None) -> list[os.DirEntry[str]]:
    """List directory entries safely, absorbing Windows PermissionError/OSError."""
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
        return entries
    except PermissionError as exc:
        msg = f"permission_denied: {normalize_source_path(path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        return []
    except OSError as exc:
        logger.debug("scandir failed for %s: %s", path, exc)
        if errors is not None and safe_exists(path):
            errors.append(f"scandir_failed: {normalize_source_path(path)} ({exc})")
        return []


def safe_walk_files(
    root: Path,
    *,
    predicate: Callable[[Path], bool] | None = None,
    skip_dir_names: frozenset[str] = frozenset(),
    errors: list[str] | None = None,
) -> list[Path]:
    """Recursively collect regular files under *root*, tolerating Windows ACL errors."""
    results: list[Path] = []
    if safe_is_file(root):
        if predicate is None or predicate(root):
            results.append(root)
        return results
    if not safe_is_dir(root):
        return results

    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        for entry in safe_scandir(current, errors=errors):
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in skip_dir_names:
                        stack.append(entry_path)
                elif entry.is_file(follow_symlinks=False) and (
                    predicate is None or predicate(entry_path)
                ):
                    results.append(entry_path)
            except PermissionError as exc:
                msg = f"permission_denied: {normalize_source_path(entry_path)} ({exc})"
                logger.warning(msg)
                if errors is not None:
                    errors.append(msg)
            except OSError as exc:
                logger.debug("walk error on %s: %s", entry_path, exc)
    results.sort()
    return results


def safe_read_text(path: Path, *, errors: list[str] | None = None) -> str | None:
    """Read UTF-8 text safely, returning None on PermissionError or OSError."""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except PermissionError as exc:
        msg = f"permission_denied: {normalize_source_path(path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        return None
    except OSError as exc:
        msg = f"read_failed: {normalize_source_path(path)} ({exc})"
        logger.debug(msg)
        if errors is not None:
            errors.append(msg)
        return None


def safe_read_bytes(path: Path, *, errors: list[str] | None = None) -> bytes | None:
    """Read raw bytes safely, returning None on PermissionError or OSError."""
    try:
        return path.read_bytes()
    except PermissionError as exc:
        msg = f"permission_denied: {normalize_source_path(path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        return None
    except OSError as exc:
        msg = f"read_failed: {normalize_source_path(path)} ({exc})"
        logger.debug(msg)
        if errors is not None:
            errors.append(msg)
        return None


@contextmanager
def open_sqlite_readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a potentially locked/WAL SQLite database safely via a temp-dir copy.

    Copies the database plus sibling ``-wal`` and ``-shm`` files into a temporary
    directory so live IDEs (Cursor, Windsurf, VS Code, Codex, OpenCode) never
    experience lock contention and source timestamps/bytes remain untouched.
    """
    if not safe_is_file(db_path):
        raise FileNotFoundError(f"SQLite file not found: {db_path}")

    with tempfile.TemporaryDirectory(prefix="omnimex_ro_sqlite_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        copied_db = tmp_root / db_path.name
        shutil.copy2(db_path, copied_db)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if safe_is_file(sidecar):
                with suppress(OSError):
                    shutil.copy2(sidecar, tmp_root / sidecar.name)

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(copied_db.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            conn = sqlite3.connect(str(copied_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def sqlite_effective_mtime_ns(db_path: Path) -> int:
    """Return max mtime_ns across a SQLite file and its ``-wal`` sidecar."""
    max_ns = 0
    for candidate in (db_path, Path(f"{db_path}-wal")):
        st = safe_stat(candidate)
        if st is not None and st.st_mtime_ns > max_ns:
            max_ns = st.st_mtime_ns
    return max_ns


def ide_user_roots(ide_names: Sequence[str]) -> list[Path]:
    """Return standard VS Code-family ``User`` directories across Windows/POSIX/macOS."""
    home = Path.home()
    roots: list[Path] = []
    appdata = os.getenv("APPDATA")
    xdg_cfg = os.getenv("XDG_CONFIG_HOME")
    base_dirs: list[Path] = []
    if appdata:
        base_dirs.append(Path(appdata))
    base_dirs.append(home / "AppData" / "Roaming")
    if xdg_cfg:
        base_dirs.append(Path(xdg_cfg))
    base_dirs.append(home / ".config")
    base_dirs.append(home / "Library" / "Application Support")

    seen: set[str] = set()
    for base in base_dirs:
        for ide in ide_names:
            candidate = base / ide / "User"
            key = normalize_source_path(candidate).lower()
            if key not in seen:
                seen.add(key)
                roots.append(candidate)
    return roots


def read_workspace_json(path: Path) -> str | None:
    """Extract normalized workspace path from sibling ``workspace.json`` if present."""
    ws_dir = path.parent if safe_is_file(path) else path
    if ws_dir.name == "chatSessions":
        ws_dir = ws_dir.parent
    ws_json = ws_dir / "workspace.json"
    if not safe_is_file(ws_json):
        return None
    raw = safe_read_text(ws_json)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("folder", "workspace", "configuration"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return decode_file_uri(val)
    return None


def query_vscdb_rows(
    db_path: Path,
    table: str,
    *,
    key_prefixes: Sequence[str] = (),
    exact_keys: Sequence[str] = (),
    errors: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Query key/value pairs from a VS Code ``state.vscdb`` table via read-only temp copy."""
    results: list[tuple[str, str]] = []
    try:
        with open_sqlite_readonly(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                return []

            clauses: list[str] = []
            params: list[str] = []
            for key in exact_keys:
                clauses.append("key = ?")
                params.append(key)
            for prefix in key_prefixes:
                clauses.append("key LIKE ?")
                params.append(f"{prefix}%")

            where_sql = f"WHERE {' OR '.join(clauses)}" if clauses else ""
            sql = f"SELECT key, CAST(value AS TEXT) AS text_val FROM {table} {where_sql}"  # noqa: S608
            for row in conn.execute(sql, params):
                val = row["text_val"]
                if isinstance(val, str) and val:
                    results.append((str(row["key"]), val))
    except (sqlite3.Error, OSError) as exc:
        msg = f"sqlite_read_failed: {normalize_source_path(db_path)} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
    return results


def parse_vscdb_bubbles(
    bubbles: Sequence[Any],
    session_id: str,
    start_idx: int = 0,
) -> tuple[list[NormalizedTurn], str | None]:
    """Parse VS Code/Cursor bubble dicts into ``(turns, detected_workspace)``."""
    turns: list[NormalizedTurn] = []
    workspace: str | None = None
    turn_idx = start_idx

    for bubble in bubbles:
        if not isinstance(bubble, dict):
            continue
        if workspace is None:
            if isinstance(bubble.get("workspaceProjectDir"), str):
                workspace = normalize_workspace_path(bubble["workspaceProjectDir"])
            elif isinstance(bubble.get("workspaceUris"), list):
                for uri in bubble["workspaceUris"]:
                    if isinstance(uri, str):
                        workspace = decode_file_uri(uri)
                        if workspace:
                            break

        btype = bubble.get("type")
        if btype in (1, "user", "human"):
            role = "user"
        elif btype in (2, "ai", "assistant", "model", "bot"):
            role = "assistant"
        elif isinstance(btype, str) and btype.lower() in {"system", "tool"}:
            role = btype.lower()
        else:
            role = "assistant"

        text = ""
        for field_name in ("text", "rawText", "content", "message"):
            val = bubble.get(field_name)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
            if isinstance(val, list):
                extracted, _ = extract_text_blocks(val)
                if extracted:
                    text = extracted
                    break
        if not text:
            continue

        model_name: str | None = None
        if isinstance(bubble.get("modelInfo"), dict):
            model_name = bubble["modelInfo"].get("modelName")
        if not model_name:
            raw_model = bubble.get("modelType") or bubble.get("model")
            if isinstance(raw_model, str) and raw_model.strip():
                model_name = raw_model.strip()

        turns.append(
            NormalizedTurn(
                session_id=session_id,
                turn_index=turn_idx,
                role=role,
                content=text,
                timestamp=parse_timestamp(bubble.get("timestamp") or bubble.get("createdAt")),
                model=model_name,
            )
        )
        turn_idx += 1

    return turns, workspace


def parse_timestamp(value: Any) -> datetime | None:
    """Parse epoch seconds/ms/us, ISO-8601 strings, or datetimes into UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        if num >= 1e14:
            num /= 1_000_000.0
        elif num >= 1e11:
            num /= 1_000.0
        try:
            return datetime.fromtimestamp(num, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_timestamp(int(text))
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return None


def extract_text_blocks(content: Any) -> tuple[str, str | None]:
    """Flatten string or structured content blocks into ``(text, thinking)``."""
    if isinstance(content, str):
        return content.strip(), None
    if not isinstance(content, list):
        return "", None

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                text_parts.append(stripped)
            continue
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").lower()
        if block_type == "thinking":
            thought = str(item.get("thinking") or item.get("text") or "").strip()
            if thought:
                thinking_parts.append(thought)
        elif block_type in ("", "text", "input_text", "output_text", "summary_text"):
            piece = item.get("text")
            if isinstance(piece, str) and piece.strip():
                text_parts.append(piece.strip())
    return "\n".join(text_parts), ("\n".join(thinking_parts) if thinking_parts else None)


def make_title(text: str | None, fallback: str = "Untitled Session", max_len: int = 100) -> str:
    """Create a concise single-line title from the first non-empty line."""
    if text:
        for line in text.splitlines():
            clean = line.strip()
            if clean:
                if len(clean) > max_len:
                    return clean[:max_len]
                return clean
    return fallback[:max_len]


def decode_file_uri(uri: str | None) -> str | None:
    """Decode a ``file:///`` or ``vscode-remote://`` URI into a normalized path."""
    if not uri or not isinstance(uri, str):
        return None
    raw = uri.strip()
    if not raw:
        return None
    if raw.startswith("vscode-remote://"):
        without_scheme = raw[len("vscode-remote://") :]
        slash_idx = without_scheme.find("/")
        if slash_idx == -1:
            return None
        decoded = unquote(without_scheme[slash_idx:])
        return normalize_workspace_path(decoded)
    if _ENV_PATH_RE.match(raw):
        decoded = unquote(raw[len("file://") :])
        decoded = _DRIVE_LEADING_SLASH_RE.sub(r"\1", decoded)
        return normalize_workspace_path(decoded)
    return normalize_workspace_path(unquote(raw))


def parse_yaml_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """Parse optional YAML frontmatter safely using python-frontmatter/PyYAML."""
    try:
        post = frontmatter.loads(raw_text)
        meta = dict(post.metadata) if isinstance(post.metadata, dict) else {}
        return meta, str(post.content or "").strip()
    except (yaml.YAMLError, ValueError, TypeError):
        return {}, raw_text.strip()


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def encode_cursor_state(
    *,
    turns: int = 0,
    external_id: str | None = None,
    workspace: str | None = None,
    model: str | None = None,
    title: str | None = None,
    digest: str | None = None,
) -> str:
    """Serialize compact incremental cursor metadata into ``IngestCursor.checksum``."""
    payload: dict[str, Any] = {"turns": turns}
    if external_id:
        payload["sid"] = external_id
    if workspace:
        payload["ws"] = workspace
    if model:
        payload["model"] = model
    if title:
        payload["title"] = title
    if digest:
        payload["sha256"] = digest
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def decode_cursor_state(checksum: str | None) -> dict[str, Any]:
    """Decode ``IngestCursor.checksum`` metadata when stored by JSONL incremental readers."""
    if not checksum:
        return {}
    stripped = checksum.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return {}
    return {}


def read_jsonl_incremental(
    path: Path,
    agent: str,
    cursor: IngestCursor | None = None,
    *,
    errors: list[str] | None = None,
) -> tuple[list[tuple[int, dict[str, Any]]], IngestCursor, bool, bool]:
    """Read newly appended JSONL records using byte_offset and line_number cursors.

    Returns ``(records, next_cursor, did_read, was_reset)``.
    Incomplete trailing lines without a newline are left unconsumed so the next
    incremental pass parses them cleanly once the writer finishes flushing.
    """
    st = safe_stat(path, errors=errors)
    norm_path = normalize_source_path(path)
    if st is None:
        empty_cursor = cursor or IngestCursor(source_path=norm_path, agent=agent)
        return [], empty_cursor, False, False

    file_size = st.st_size
    mtime_ns = st.st_mtime_ns

    if (
        cursor is not None
        and cursor.file_size == file_size
        and cursor.mtime_ns == mtime_ns
    ):
        return [], cursor, False, False

    was_reset = cursor is None or file_size < cursor.byte_offset
    start_offset = 0 if was_reset else cursor.byte_offset
    line_num = 0 if was_reset else cursor.line_number

    records: list[tuple[int, dict[str, Any]]] = []
    current_offset = start_offset

    try:
        fh_ctx = path.open("rb")
    except PermissionError as exc:
        msg = f"permission_denied: {norm_path} ({exc})"
        logger.warning(msg)
        if errors is not None:
            errors.append(msg)
        fallback = cursor or IngestCursor(source_path=norm_path, agent=agent)
        return [], fallback, False, False
    except OSError as exc:
        logger.warning("Cannot open JSONL %s: %s", path, exc)
        if errors is not None:
            errors.append(f"open_failed: {norm_path} ({exc})")
        fallback = cursor or IngestCursor(source_path=norm_path, agent=agent)
        return [], fallback, False, False

    with fh_ctx as fh:
        if start_offset > 0:
            fh.seek(start_offset)
        while True:
            raw_line = fh.readline()
            if not raw_line:
                break
            terminated = raw_line.endswith(b"\n")
            text = raw_line
            if current_offset == 0 and text.startswith(b"\xef\xbb\xbf"):
                text = text[3:]
            decoded = text.decode("utf-8", errors="replace").strip()

            if not decoded:
                current_offset += len(raw_line)
                line_num += 1
                continue

            try:
                parsed = json.loads(decoded)
            except json.JSONDecodeError:
                if not terminated:
                    # Leave incomplete trailing write unconsumed for next incremental pass.
                    break
                current_offset += len(raw_line)
                line_num += 1
                continue

            current_offset += len(raw_line)
            line_num += 1
            if isinstance(parsed, dict):
                records.append((line_num, parsed))

    next_cursor = IngestCursor(
        source_path=norm_path,
        agent=agent,
        byte_offset=current_offset,
        line_number=line_num,
        mtime_ns=mtime_ns,
        file_size=file_size,
        checksum=cursor.checksum if (cursor and not was_reset) else None,
    )
    return records, next_cursor, True, was_reset


def normalize_session_timestamps(
    turns: Sequence[NormalizedTurn],
    *,
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
    fallback_mtime_ns: int | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Compute consistent started_at / updated_at bounds for a parsed session."""
    turn_times = [t.timestamp for t in turns if t.timestamp is not None]
    start = started_at or (min(turn_times) if turn_times else None)
    end = updated_at or (max(turn_times) if turn_times else None)
    if start is None and fallback_mtime_ns:
        start = datetime.fromtimestamp(fallback_mtime_ns / 1_000_000_000, tz=UTC)
    if end is None:
        end = start
    if start is not None and end is not None and end < start:
        end = start
    return start, end


class BaseAgentAdapter(ABC):
    """Abstract base class for all Omnimex coding-agent session adapters."""

    agent_id: str = ""
    display_name: str = ""
    supports_native_memories: bool = False

    def __init__(self, search_roots: Sequence[str | Path] | None = None) -> None:
        self._explicit_roots: list[Path] | None = (
            [Path(r).expanduser() for r in search_roots] if search_roots is not None else None
        )
        self.last_errors: list[str] = []

    def resolve_roots(self, roots: Sequence[Path] | None = None) -> list[Path]:
        """Return explicit scan roots if provided, else constructor roots or defaults."""
        if roots is not None:
            return [Path(r).expanduser() for r in roots]
        if self._explicit_roots is not None:
            return list(self._explicit_roots)
        return self.default_roots()

    @abstractmethod
    def default_roots(self) -> list[Path]:
        """Return default local OS discovery directories/files for this agent."""

    @abstractmethod
    def detect_sources(self, roots: Sequence[Path] | None = None) -> list[Path]:
        """Discover readable session source files for this agent."""

    @abstractmethod
    def parse_file(self, path: Path) -> list[NormalizedSession]:
        """Parse a single discovered source file into normalized sessions."""

    def parse_file_incremental(
        self,
        path: Path,
        cursor: IngestCursor | None = None,
        *,
        start_turn_index: int | None = None,
    ) -> IncrementalParseResult:
        """Incrementally parse *path* against *cursor* (monolithic snapshot default)."""
        norm_path = normalize_source_path(path)
        st = safe_stat(path, errors=self.last_errors)
        if st is None:
            fallback_cursor = cursor or IngestCursor(source_path=norm_path, agent=self.agent_id)
            return IncrementalParseResult(
                sessions=[],
                cursor=fallback_cursor,
                changed=False,
                is_append_only=False,
            )

        effective_mtime = (
            sqlite_effective_mtime_ns(path)
            if path.suffix.lower() in {".db", ".sqlite", ".vscdb"}
            else st.st_mtime_ns
        )

        if (
            cursor is not None
            and cursor.file_size == st.st_size
            and cursor.mtime_ns == effective_mtime
        ):
            return IncrementalParseResult(
                sessions=[],
                cursor=cursor,
                changed=False,
                is_append_only=False,
            )

        raw_bytes = safe_read_bytes(path, errors=self.last_errors)
        digest = sha256_bytes(raw_bytes) if raw_bytes is not None else None
        if cursor is not None and digest is not None:
            state = decode_cursor_state(cursor.checksum)
            prev_digest = state.get("sha256") or cursor.checksum
            if prev_digest == digest and cursor.file_size == st.st_size:
                updated = cursor.model_copy(update={"mtime_ns": effective_mtime})
                return IncrementalParseResult(
                    sessions=[],
                    cursor=updated,
                    changed=False,
                    is_append_only=False,
                )

        sessions = self.parse_file(path)
        next_cursor = IngestCursor(
            source_path=norm_path,
            agent=self.agent_id,
            byte_offset=st.st_size,
            line_number=sum(len(s.turns) for s in sessions),
            mtime_ns=effective_mtime,
            file_size=st.st_size,
            checksum=digest,
        )
        return IncrementalParseResult(
            sessions=sessions,
            cursor=next_cursor,
            changed=True,
            is_append_only=False,
        )

    def _parse_jsonl_incremental(
        self,
        path: Path,
        cursor: IngestCursor | None,
        parse_records: Callable[
            [Path, list[tuple[int, dict[str, Any]]], int, dict[str, Any]],
            list[NormalizedSession],
        ],
        *,
        start_turn_index: int | None = None,
    ) -> IncrementalParseResult:
        """Shared byte/line incremental parser for append-only JSONL adapters."""
        records, next_cursor, did_read, was_reset = read_jsonl_incremental(
            path, self.agent_id, cursor, errors=self.last_errors
        )
        if not did_read:
            return IncrementalParseResult(
                sessions=[],
                cursor=next_cursor,
                changed=False,
                is_append_only=not was_reset,
            )

        prev_state = {} if (was_reset or cursor is None) else decode_cursor_state(cursor.checksum)
        base_turn_idx = (
            start_turn_index
            if start_turn_index is not None
            else (0 if was_reset else int(prev_state.get("turns", 0)))
        )

        sessions = parse_records(path, records, base_turn_idx, prev_state)
        emitted_turns = sum(len(s.turns) for s in sessions)
        first = sessions[0] if sessions else None
        next_cursor = next_cursor.model_copy(
            update={
                "checksum": encode_cursor_state(
                    turns=base_turn_idx + emitted_turns,
                    external_id=(first.external_id if first else prev_state.get("sid")),
                    workspace=(first.workspace if first else prev_state.get("ws")),
                    model=(first.model if first else prev_state.get("model")),
                    title=(first.title if first else prev_state.get("title")),
                )
            }
        )
        return IncrementalParseResult(
            sessions=sessions,
            cursor=next_cursor,
            changed=bool(sessions) or (cursor is None) or next_cursor.byte_offset != cursor.byte_offset,
            is_append_only=not was_reset,
        )

    def iter_sessions(
        self,
        *,
        since: datetime | None = None,
        roots: Sequence[Path] | None = None,
    ) -> Iterator[NormalizedSession]:
        """Yield normalized sessions across all detected sources."""
        self.last_errors.clear()
        since_ns = int(since.timestamp() * 1_000_000_000) - 1_000_000_000 if since else None
        for source in self.detect_sources(roots=roots):
            if since_ns is not None:
                mtime_ns = (
                    sqlite_effective_mtime_ns(source)
                    if source.suffix.lower() in {".db", ".sqlite", ".vscdb"}
                    else (st.st_mtime_ns if (st := safe_stat(source, errors=self.last_errors)) else 0)
                )
                if mtime_ns and mtime_ns < since_ns:
                    continue
            try:
                yield from self.parse_file(source)
            except PermissionError as exc:
                msg = f"permission_denied: {normalize_source_path(source)} ({exc})"
                logger.warning(msg)
                self.last_errors.append(msg)
            except OSError as exc:
                msg = f"parse_io_error: {normalize_source_path(source)} ({exc})"
                logger.warning(msg)
                self.last_errors.append(msg)
            except Exception as exc:  # noqa: BLE001
                msg = f"parse_error: {normalize_source_path(source)} ({exc})"
                logger.warning(msg)
                self.last_errors.append(msg)

    def scan_sessions(
        self,
        *,
        since: datetime | None = None,
        roots: Sequence[Path] | None = None,
    ) -> list[NormalizedSession]:
        """Collect all sessions yielded by ``iter_sessions``."""
        return list(self.iter_sessions(since=since, roots=roots))

    def discover_native_memories(self, roots: Sequence[Path] | None = None) -> list[MemoryCard]:
        """Discover native agent memory files/entries if supported."""
        return []

    def matches_path(self, path: Path) -> bool:
        """Return True if *path* belongs to a source format handled by this adapter."""
        return False

    def health_status(self, roots: Sequence[Path] | None = None) -> AdapterSourceStatus:
        """Inspect roots and source files and summarize adapter health."""
        self.last_errors.clear()
        resolved = self.resolve_roots(roots)
        sources = self.detect_sources(roots=resolved)
        memories = self.discover_native_memories(roots=resolved) if self.supports_native_memories else []
        perm_errors = [e for e in self.last_errors if e.startswith("permission_denied:")]

        if perm_errors and not sources and not memories:
            state: HealthState = "permission_denied"
        elif self.last_errors:
            state = "degraded"
        elif sources or memories or any(safe_exists(r) for r in resolved):
            state = "healthy"
        else:
            state = "not_found"

        return AdapterSourceStatus(
            agent=self.agent_id,
            name=self.display_name,
            status=state,
            source_paths=[normalize_source_path(p) for p in resolved],
            discovered_files=len(sources),
            native_memory_files=len(memories),
            errors=list(self.last_errors),
            supports_native_memories=self.supports_native_memories,
        )


__all__ = [
    "AdapterSourceStatus",
    "BaseAgentAdapter",
    "HealthState",
    "IncrementalParseResult",
    "decode_cursor_state",
    "decode_file_uri",
    "encode_cursor_state",
    "extract_text_blocks",
    "ide_user_roots",
    "make_title",
    "normalize_session_timestamps",
    "normalize_source_path",
    "open_sqlite_readonly",
    "parse_timestamp",
    "parse_vscdb_bubbles",
    "parse_yaml_frontmatter",
    "query_vscdb_rows",
    "read_jsonl_incremental",
    "read_workspace_json",
    "safe_exists",
    "safe_is_dir",
    "safe_is_file",
    "safe_read_bytes",
    "safe_read_text",
    "safe_scandir",
    "safe_stat",
    "safe_walk_files",
    "sha256_bytes",
    "sqlite_effective_mtime_ns",
]
