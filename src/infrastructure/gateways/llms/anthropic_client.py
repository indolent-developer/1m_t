"""
gateways.llm.anthropic_client — AnthropicLLM

Implements ILLMClient using the Anthropic Python SDK.

Setup:
    pip install anthropic
    export ANTHROPIC_API_KEY="sk-ant-..."
"""
from __future__ import annotations

import os

from core.adapters.llm import ILLMClient, LLMRequest, LLMResponse

DEFAULT_MODEL = "claude-opus-4-8"


class AnthropicLLM(ILLMClient):

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        try:
            import anthropic
            self._anthropic = anthropic
        except ImportError:
            raise ImportError("AnthropicLLM requires 'anthropic' — pip install anthropic")

        self._api_key      = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._default_model = default_model

        if not self._api_key:
            raise ValueError("Anthropic API key required — pass api_key or set ANTHROPIC_API_KEY")

        self._client = self._anthropic.AsyncAnthropic(api_key=self._api_key)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._default_model

        kwargs: dict = dict(
            model=model,
            max_tokens=request.max_tokens,
            messages=[{"role": "user", "content": request.prompt}],
        )
        if request.system:
            kwargs["system"] = request.system

        message = await self._client.messages.create(**kwargs)

        return LLMResponse(
            text=message.content[0].text,
            model=message.model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
