"""LLM provider interface for Vesma.

All providers must implement this interface for use in the synthesis
pipeline (M4) and context filter (M10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send a completion request and return the response."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider identifier (e.g. 'anthropic', 'ollama')."""
        ...


def create_provider(config: object) -> LLMProvider:
    """Factory: instantiate the configured LLM provider.

    TODO (M4): implement provider registry.
    """
    raise NotImplementedError("LLM provider factory not yet implemented (M4)")
