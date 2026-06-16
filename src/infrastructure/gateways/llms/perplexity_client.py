"""
gateways.llm.perplexity_client — PerplexityLLM

Implements ILLMClient using Perplexity's OpenAI-compatible API.
Perplexity's sonar models are web-search grounded — useful for
real-time catalyst research alongside scanner output.

Setup:
    pip install openai
    export PERPLEXITY_API_KEY="pplx-..."

Models:
    sonar              — fast, grounded search
    sonar-pro          — higher quality, larger context
    sonar-reasoning    — chain-of-thought + search
    sonar-reasoning-pro — deepest reasoning + search
"""
from __future__ import annotations

import os

from core.adapters.llm import ILLMClient, LLMRequest, LLMResponse

DEFAULT_MODEL = "sonar-pro"
BASE_URL      = "https://api.perplexity.ai"


class PerplexityLLM(ILLMClient):

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        try:
            from openai import AsyncOpenAI
            self._AsyncOpenAI = AsyncOpenAI
        except ImportError:
            raise ImportError("PerplexityLLM requires 'openai' — pip install openai")

        self._api_key       = api_key or os.environ.get("PERPLEXITY_API_KEY")
        self._default_model = default_model

        if not self._api_key:
            raise ValueError("Perplexity API key required — pass api_key or set PERPLEXITY_API_KEY")

        self._client = self._AsyncOpenAI(
            api_key=self._api_key,
            base_url=BASE_URL,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model    = request.model or self._default_model
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

        usage  = response.usage
        return LLMResponse(
            text=response.choices[0].message.content,
            model=response.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
