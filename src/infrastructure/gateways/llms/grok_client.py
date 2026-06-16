"""
gateways.llm.grok_client — GrokLLM

Implements ILLMClient using xAI's API (OpenAI-compatible).

Setup:
    pip install openai
    export XAI_API_KEY="xai-..."
"""
from __future__ import annotations

import os

from core.adapters.llm import ILLMClient, LLMRequest, LLMResponse

DEFAULT_MODEL  = "grok-3"
XAI_BASE_URL   = "https://api.x.ai/v1"


class GrokLLM(ILLMClient):

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        try:
            from openai import AsyncOpenAI
            self._AsyncOpenAI = AsyncOpenAI
        except ImportError:
            raise ImportError("GrokLLM requires 'openai' — pip install openai")

        self._api_key       = api_key or os.environ.get("XAI_API_KEY")
        self._default_model = default_model

        if not self._api_key:
            raise ValueError("xAI API key required — pass api_key or set XAI_API_KEY")

        self._client = self._AsyncOpenAI(
            api_key=self._api_key,
            base_url=XAI_BASE_URL,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._default_model

        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )

        choice = response.choices[0]
        usage  = response.usage

        return LLMResponse(
            text=choice.message.content,
            model=response.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
