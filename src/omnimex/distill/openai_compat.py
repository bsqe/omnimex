"""OpenAI-compatible LLM memory distiller."""

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


class OpenAICompatibleDistiller(BaseDistiller):
    """Distiller calling any OpenAI-compatible ``/chat/completions`` endpoint."""

    provider_name = "openai_compatible"
    target_status = "llm_distilled"

    def __init__(
        self,
        *,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
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
        raw_content = self._call_completions(user_prompt)
        return parse_llm_memory_cards(raw_content, session, distill_status="llm_distilled")

    def _call_completions(self, user_prompt: str) -> str:
        url = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else f"{self.base_url}/chat/completions"
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key and self.api_key.strip():
            headers["Authorization"] = f"Bearer {self.api_key.strip()}"

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": _DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            return str(choices[0].get("message", {}).get("content") or "")
        finally:
            if owns_client:
                client.close()


__all__ = ["OpenAICompatibleDistiller"]
