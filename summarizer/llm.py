"""Pluggable LLM client for the description (Phase 2) layer.

Zero-dependency: talks to any OpenAI-compatible ``/v1/chat/completions`` endpoint
(Ollama ``/v1``, llama.cpp server, vLLM, LM Studio, ...) over the stdlib
``urllib``. The project intentionally avoids adding ``requests``/``httpx``.

Configuration is environment-driven so the offline ``describe`` step and the MCP
server pick it up the same way:

    LEGACY_REVERSE_LLM_BASE_URL   e.g. http://localhost:11434/v1  (empty => disabled)
    LEGACY_REVERSE_LLM_MODEL      default: qwen3-coder-next
    LEGACY_REVERSE_LLM_API_KEY    optional bearer token
    LEGACY_REVERSE_LLM_LANG       default: ru  (language for generated text)
    LEGACY_REVERSE_LLM_TIMEOUT    seconds per request, default: 60
    LEGACY_REVERSE_LLM_MAX_TOKENS default: 512
    LEGACY_REVERSE_LLM_TEMPERATURE default: 0.1

When no base URL is configured the client is *disabled*: callers must fall back
to deterministic descriptions. Any network/parse error returns ``None`` (never
raises into the pipeline), so a flaky local endpoint degrades gracefully.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class LLMConfig:
    base_url: str | None = None
    model: str = "qwen3-coder-next"
    api_key: str | None = None
    lang: str = "ru"
    timeout: float = 60.0
    max_tokens: int = 512
    temperature: float = 0.1

    @classmethod
    def from_env(cls) -> "LLMConfig":
        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        def _i(name: str, default: int) -> int:
            raw = os.environ.get(name)
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        base = (os.environ.get("LEGACY_REVERSE_LLM_BASE_URL") or "").strip().rstrip("/")
        return cls(
            base_url=base or None,
            model=os.environ.get("LEGACY_REVERSE_LLM_MODEL") or "qwen3-coder-next",
            api_key=os.environ.get("LEGACY_REVERSE_LLM_API_KEY") or None,
            lang=os.environ.get("LEGACY_REVERSE_LLM_LANG") or "ru",
            timeout=_f("LEGACY_REVERSE_LLM_TIMEOUT", 60.0),
            max_tokens=_i("LEGACY_REVERSE_LLM_MAX_TOKENS", 512),
            temperature=_f("LEGACY_REVERSE_LLM_TEMPERATURE", 0.1),
        )


class LLMClient:
    """Thin OpenAI-compatible chat client. Disabled if no base URL is set."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()

    @property
    def enabled(self) -> bool:
        return bool(self.config.base_url)

    def describe(self) -> str:
        """Human-readable provenance string for the ``summary.model`` column."""
        return self.config.model if self.enabled else "deterministic"

    def complete(self, *, system: str, user: str) -> str | None:
        """One chat completion. Returns assistant text, or ``None`` on any failure
        (disabled client, network error, bad status, unparseable body)."""
        if not self.enabled:
            return None

        url = f"{self.config.base_url}/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return None

        try:
            obj = json.loads(body)
            text = obj["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
        text = (text or "").strip()
        return text or None
