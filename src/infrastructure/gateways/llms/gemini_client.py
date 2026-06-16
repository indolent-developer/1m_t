"""
gateways.llm.gemini_client — GeminiLLM

Implements ILLMClient using Google's GenAI SDK.

Setup:
    pip install google-genai
    export GEMINI_API_KEY="AIza..."
"""
from __future__ import annotations

import os

from core.adapters.llm import ILLMClient, LLMRequest, LLMResponse

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiLLM(ILLMClient):

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        try:
            from google import genai
            self._genai = genai
        except ImportError:
            raise ImportError("GeminiLLM requires 'google-genai' — pip install google-genai")

        self._api_key       = api_key or os.environ.get("GEMINI_API_KEY")
        self._default_model = default_model

        if not self._api_key:
            raise ValueError("Gemini API key required — pass api_key or set GEMINI_API_KEY")

        self._client = self._genai.Client(api_key=self._api_key)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model   = request.model or self._default_model
        content = request.prompt
        if request.system:
            content = f"{request.system}\n\n{request.prompt}"

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=content,
        )

        usage = response.usage_metadata
        return LLMResponse(
            text=response.text,
            model=model,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=usage.candidates_token_count or 0,
        )
