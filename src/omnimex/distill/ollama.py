"""Local Ollama distiller (supports /api/chat and /v1/chat/completions)."""

from __future__ import annotations

from typing import Any

import httpx

from omnimex.core.models import MemoryCard, NormalizedSession
from omnimex.distill.base import (
    _DISTILL_SYSTEM_PROMPT,
    BaseDistiller,
    format_session_for_distill,
    parse_llm_memory_cards,
)


class OllamaDistiller(BaseDistiller):
    """Distiller calling a local Ollama server at ``http://127.0.0.1:11434``."""

    provider_name = "ollama"
    target_status = "llm_distilled"

    def __init__(
        self,
        *,
        model: str = "qwen2.5:7b-instruct",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client

    def distill_session(self, session: NormalizedSession) -> list[MemoryCard]:
        transcript = format_session_for_distill(session)
        if not transcript.strip():
            return []

        user_prompt = (
            "Extract durable preference, decision, pitfall, and fact memory cards from this session:\n\n"
            f"{transcript}"
        )
        raw_content = self._call_ollama(user_prompt)
        return parse_llm_memory_cards(raw_content, session, distill_status="llm_distilled")

    def _call_ollama(self, user_prompt: str) -> str:
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            if self.base_url.endswith("/v1"):
                url = f"{self.base_url}/chat/completions"
                payload: dict[str, Any] = {
                    "model": self.model,
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                }
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if choices:
                    return str(choices[0].get("message", {}).get("content") or "")
                return ""

            url = f"{self.base_url}/api/chat"
            payload = {
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"temperature": 0.1},
            }
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("message", {}).get("content") or "")
        finally:
            if owns_client:
                client.close()


__all__ = ["OllamaDistiller"]
