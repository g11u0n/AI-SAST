"""Provider-neutral interface used by the runtime agents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Sequence


class ProviderError(RuntimeError):
    """A fail-closed error raised by an LLM provider."""


class LLMProvider(ABC):
    """Minimal provider boundary required by the v1 runtime."""

    @abstractmethod
    def chat(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        response_schema: Mapping[str, Any] | None,
        options: Mapping[str, Any],
        stream: bool,
        keep_alive: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
        component_payloads: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run one stateless chat request and return the raw provider object."""
