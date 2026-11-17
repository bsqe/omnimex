"""Secret scrubbing and config/URL redaction inspired by basic-memory."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from omnimex.core.models import (
    HandoffBrief,
    MemoryCard,
    NormalizedSession,
    NormalizedTurn,
    ToolCallRecord,
)

# Fields in configs that contain secrets and must never be surfaced.
SECRET_FIELDS = frozenset(
    {
        "api_key",
        "distiller_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "auth_token",
        "token",
        "secret",
        "password",
    }
)

# Fields whose values are URLs that may embed user:password credentials.
URL_FIELDS = frozenset(
    {
        "database_url",
        "base_url",
        "distiller_base_url",
        "ollama_base_url",
        "redis_url",
    }
)

_SECRET_QUERY_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "app_key",
        "auth_key",
        "client_key",
        "consumer_key",
        "credential",
        "encryption_key",
        "master_key",
        "passwd",
        "password",
        "private_key",
        "pwd",
        "secret",
        "secret_key",
        "session_key",
        "signing_key",
        "sslpassword",
        "token",
    }
)

_SECRET_KEY_SUFFIXES = (
    "_password",
    "_passwd",
    "_pwd",
    "_secret",
    "_token",
    "_api_key",
    "_secret_key",
    "_access_key",
    "_private_key",
    "_client_key",
    "_auth_key",
    "_encryption_key",
    "_signing_key",
    "_master_key",
    "_session_key",
    "_consumer_key",
    "_app_key",
)

# Order matters: scrub multi-line private keys & URLs first, then structured tokens,
# then generic key=value secret assignments.
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
    r"|-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[^\r\n]*"
)

_URL_WITH_CREDENTIALS_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqp|amqps|mssql|sqlite|https?|ftp)://"
    r"[^\s:@\"'`<>]+:[^\s@\"'`<>]+@[^\s\"'`<>]+",
    re.IGNORECASE,
)

_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)

_AUTH_BEARER_HEADER_RE = re.compile(
    r"(?i)(\bAuthorization\s*[:=]\s*Bearer\s+)(?!\[REDACTED)([A-Za-z0-9._~+/=-]{8,})"
)

_AUTH_BASIC_HEADER_RE = re.compile(
    r"(?i)(\bAuthorization\s*[:=]\s*Basic\s+)(?!\[REDACTED)([A-Za-z0-9+/=]{8,})"
)

_BEARER_TOKEN_RE = re.compile(
    r"(?i)(\bBearer\s+)(?!\[REDACTED)(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{12,}"
)

_OPENAI_ANTHROPIC_SK_RE = re.compile(
    r"(?<![A-Za-z0-9_-])sk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{12,}\b"
)

_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"
)

_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b")

_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")

_OTHER_VENDOR_KEY_RE = re.compile(
    r"\b(?:hf_[A-Za-z0-9]{16,}|pplx-[A-Za-z0-9]{16,}|gsk_[A-Za-z0-9]{16,}|"
    r"npm_[A-Za-z0-9]{16,}|pypi-[A-Za-z0-9_-]{20,})\b"
)

_ENV_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b[A-Z0-9_]*(?:API_KEY|SECRET_KEY|ACCESS_KEY|AUTH_TOKEN|ACCESS_TOKEN|"
    r"PRIVATE_KEY|CLIENT_SECRET|DB_PASSWORD|PASSWORD|PASSWD)\s*[:=]\s*[\"']?)"
    r"(?!\[REDACTED|\*\*\*)([^\s\"'\r\n&;]{8,})([\"']?)"
)


def _query_key_is_secret(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _SECRET_QUERY_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _redact_query_secrets(query: str) -> str:
    """Mask credential-bearing query values while preserving diagnostic options."""
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(_query_key_is_secret(key) for key, _ in pairs):
        return query
    return urlencode(
        [(key, "***" if _query_key_is_secret(key) else value) for key, value in pairs],
        safe="*",
    )


def redact_url(url: str) -> str:
    """Strip userinfo and credential-bearing query values from a URL string."""
    if "://" not in url:
        base, query_separator, query = url.partition("?")
        redacted_query = _redact_query_secrets(query)
        redacted_base = base
        if "@" in base:
            _, _, authority = base.rpartition("@")
            redacted_base = f"***@{authority}"
        if redacted_base == base and redacted_query == query:
            return url
        return f"{redacted_base}?{redacted_query}" if query_separator else redacted_base

    try:
        parsed = urlparse(url)
    except ValueError:
        base, query_separator, query = url.partition("?")
        safe_url = f"{base}?{_redact_query_secrets(query)}" if query_separator else base
        scheme, separator, remainder = safe_url.partition("://")
        if separator and "@" in remainder:
            _, _, authority = remainder.rpartition("@")
            return f"{scheme}://***@{authority}"
        return safe_url

    redacted_query = _redact_query_secrets(parsed.query)
    if "@" not in parsed.netloc and redacted_query == parsed.query:
        return url

    redacted_netloc = parsed.netloc
    if "@" in parsed.netloc:
        _, _, authority = parsed.netloc.rpartition("@")
        redacted_netloc = f"***@{authority}"

    return urlunparse(parsed._replace(netloc=redacted_netloc, query=redacted_query))


def redact_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a raw config dict with secret fields scrubbed."""
    result: dict[str, Any] = {}
    for k, v in raw.items():
        if k.lower() in SECRET_FIELDS or _query_key_is_secret(k):
            continue
        if isinstance(v, dict):
            result[k] = redact_config(v)
        elif k.lower() in URL_FIELDS and isinstance(v, str):
            result[k] = redact_url(v)
        elif isinstance(v, str):
            result[k] = redact_text(v)
        else:
            result[k] = v
    return result


def redact_text_with_flag(text: str) -> tuple[str, bool]:
    """Scrub secrets from text and report whether any replacement occurred."""
    if not text:
        return text, False

    scrubbed = text
    scrubbed = _PEM_PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", scrubbed)
    scrubbed = _URL_WITH_CREDENTIALS_RE.sub(lambda m: redact_url(m.group(0)), scrubbed)
    scrubbed = _JWT_RE.sub("[REDACTED_JWT]", scrubbed)
    scrubbed = _AUTH_BEARER_HEADER_RE.sub(r"\1[REDACTED_TOKEN]", scrubbed)
    scrubbed = _AUTH_BASIC_HEADER_RE.sub(r"\1[REDACTED_TOKEN]", scrubbed)
    scrubbed = _BEARER_TOKEN_RE.sub(r"\1[REDACTED_TOKEN]", scrubbed)
    scrubbed = _GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", scrubbed)
    scrubbed = _GOOGLE_API_KEY_RE.sub("[REDACTED_GOOGLE_KEY]", scrubbed)
    scrubbed = _AWS_KEY_RE.sub("[REDACTED_AWS_KEY]", scrubbed)
    scrubbed = _SLACK_TOKEN_RE.sub("[REDACTED_SLACK_TOKEN]", scrubbed)
    scrubbed = _OTHER_VENDOR_KEY_RE.sub("[REDACTED_API_KEY]", scrubbed)
    scrubbed = _OPENAI_ANTHROPIC_SK_RE.sub("[REDACTED_API_KEY]", scrubbed)
    scrubbed = _ENV_SECRET_ASSIGN_RE.sub(r"\1[REDACTED_SECRET]\3", scrubbed)

    return scrubbed, scrubbed != text


def redact_text(text: str) -> str:
    """Scrub secrets from arbitrary text."""
    scrubbed, _ = redact_text_with_flag(text)
    return scrubbed


def contains_secrets(text: str) -> bool:
    """Return True when text contains recognizable secrets."""
    _, changed = redact_text_with_flag(text)
    return changed


def redact_value(value: Any) -> tuple[Any, bool]:
    """Recursively scrub secrets inside strings, lists, and dicts."""
    if isinstance(value, str):
        return redact_text_with_flag(value)
    if isinstance(value, list):
        changed = False
        items: list[Any] = []
        for item in value:
            clean_item, item_changed = redact_value(item)
            changed = changed or item_changed
            items.append(clean_item)
        return items, changed
    if isinstance(value, dict):
        changed = False
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _query_key_is_secret(key) and isinstance(item, str):
                if item != "[REDACTED_SECRET]":
                    changed = True
                out[key] = "[REDACTED_SECRET]"
            else:
                clean_item, item_changed = redact_value(item)
                changed = changed or item_changed
                out[key] = clean_item
        return out, changed
    return value, False


def redact_tool_call(tool_call: ToolCallRecord) -> tuple[ToolCallRecord, bool]:
    """Return a scrubbed copy of a ToolCallRecord and whether anything changed."""
    clean_args, args_changed = redact_value(tool_call.arguments)
    clean_output = tool_call.output
    output_changed = False
    if tool_call.output is not None:
        clean_output, output_changed = redact_text_with_flag(tool_call.output)

    changed = args_changed or output_changed
    if not changed:
        return tool_call, False
    return (
        tool_call.model_copy(update={"arguments": clean_args, "output": clean_output}),
        True,
    )


def redact_turn(turn: NormalizedTurn) -> NormalizedTurn:
    """Return a copy of a NormalizedTurn with content, thinking, and tool calls scrubbed."""
    clean_content, c_changed = redact_text_with_flag(turn.content)
    clean_thinking = turn.thinking
    t_changed = False
    if turn.thinking is not None:
        clean_thinking, t_changed = redact_text_with_flag(turn.thinking)

    clean_tools: list[ToolCallRecord] = []
    tools_changed = False
    for tc in turn.tool_calls:
        clean_tc, tc_changed = redact_tool_call(tc)
        tools_changed = tools_changed or tc_changed
        clean_tools.append(clean_tc)

    any_changed = c_changed or t_changed or tools_changed
    if not any_changed and not turn.redacted:
        return turn
    return turn.model_copy(
        update={
            "content": clean_content,
            "thinking": clean_thinking,
            "tool_calls": clean_tools,
            "redacted": turn.redacted or any_changed,
        }
    )


def redact_session(session: NormalizedSession) -> NormalizedSession:
    """Return a copy of a NormalizedSession with title, summary, and turns scrubbed."""
    clean_title = redact_text(session.title)
    clean_summary = redact_text(session.summary) if session.summary is not None else None
    clean_turns = [redact_turn(t) for t in session.turns]
    return session.model_copy(
        update={
            "title": clean_title,
            "summary": clean_summary,
            "turns": clean_turns,
        }
    )


def redact_memory_card(card: MemoryCard) -> MemoryCard:
    """Return a copy of a MemoryCard with title and content scrubbed."""
    return card.model_copy(
        update={
            "title": redact_text(card.title),
            "content": redact_text(card.content),
        }
    )


def redact_handoff_brief(brief: HandoffBrief) -> HandoffBrief:
    """Return a copy of a HandoffBrief with title, markdown, and sections scrubbed."""
    sections = brief.sections.model_copy(
        update={
            "goal": redact_text(brief.sections.goal),
            "current_state": redact_text(brief.sections.current_state),
            "key_decisions": [redact_text(item) for item in brief.sections.key_decisions],
            "pitfalls_and_constraints": [
                redact_text(item) for item in brief.sections.pitfalls_and_constraints
            ],
            "next_steps": [redact_text(item) for item in brief.sections.next_steps],
        }
    )
    return brief.model_copy(
        update={
            "title": redact_text(brief.title),
            "markdown": redact_text(brief.markdown),
            "sections": sections,
            "redacted": True,
        }
    )


__all__ = [
    "SECRET_FIELDS",
    "URL_FIELDS",
    "contains_secrets",
    "redact_config",
    "redact_handoff_brief",
    "redact_memory_card",
    "redact_session",
    "redact_text",
    "redact_text_with_flag",
    "redact_tool_call",
    "redact_turn",
    "redact_url",
    "redact_value",
]
