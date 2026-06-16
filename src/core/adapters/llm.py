"""
core.adapters.llm — ILLMClient interface + request/response types.

ABC enforces implementation at runtime — instantiating a subclass that
has not implemented `complete` raises TypeError immediately.

Implementations:
    gateways.llm.anthropic_client.AnthropicLLM
    gateways.llm.grok_client.GrokLLM
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMRequest:
    prompt:      str
    system:      str | None = None   # system-level instruction (supported by Claude & Grok)
    max_tokens:  int        = 4096
    temperature: float      = 0.7
    model:       str | None = None   # None → use the client's default model


@dataclass
class LLMResponse:
    text:          str
    model:         str
    input_tokens:  int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ILLMClient(ABC):
    """
    Abstract base for all LLM clients.

    Subclasses MUST implement `complete`. Python raises TypeError at
    instantiation time if the method is missing — no silent failures.

    Usage:
        client = AnthropicLLM(api_key="sk-ant-...")
        response = await client.complete(LLMRequest(prompt="Analyse AAPL..."))
        print(response.text)
    """

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send a prompt and return the model's response."""
