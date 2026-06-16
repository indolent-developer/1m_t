"""
Tests for ILLMClient, LLMRequest, LLMResponse and all four concrete clients.

All external SDK calls are mocked — no real API keys or network needed.
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../src"))

import pytest
from core.adapters.llm import ILLMClient, LLMRequest, LLMResponse
from infrastructure.gateways.llms.anthropic_client import AnthropicLLM
from infrastructure.gateways.llms.gemini_client import GeminiLLM
from infrastructure.gateways.llms.grok_client import GrokLLM
from infrastructure.gateways.llms.perplexity_client import PerplexityLLM


# ── ILLMClient — ABC enforcement ──────────────────────────────────────────────

def test_cannot_instantiate_illlmclient_directly():
    with pytest.raises(TypeError, match="abstract"):
        ILLMClient()  # type: ignore


def test_subclass_without_complete_raises():
    class Incomplete(ILLMClient):
        pass
    with pytest.raises(TypeError, match="complete"):
        Incomplete()


def test_subclass_with_complete_instantiates():
    class Complete(ILLMClient):
        async def complete(self, request): ...
    Complete()   # must not raise


# ── LLMRequest ────────────────────────────────────────────────────────────────

def test_llm_request_defaults():
    r = LLMRequest(prompt="hello")
    assert r.prompt      == "hello"
    assert r.system      is None
    assert r.max_tokens  == 4096
    assert r.temperature == 0.7
    assert r.model       is None


def test_llm_request_custom():
    r = LLMRequest(prompt="p", system="sys", max_tokens=1000, temperature=0.1, model="x")
    assert r.system      == "sys"
    assert r.max_tokens  == 1000
    assert r.temperature == 0.1
    assert r.model       == "x"


# ── LLMResponse ───────────────────────────────────────────────────────────────

def test_llm_response_total_tokens():
    r = LLMResponse(text="hi", model="m", input_tokens=100, output_tokens=50)
    assert r.total_tokens == 150


def test_llm_response_total_tokens_zero():
    r = LLMResponse(text="", model="m", input_tokens=0, output_tokens=0)
    assert r.total_tokens == 0


# ── Shared helpers ────────────────────────────────────────────────────────────

def _assert_response(resp: LLMResponse, text: str, model: str):
    assert isinstance(resp, LLMResponse)
    assert resp.text          == text
    assert resp.model         == model
    assert resp.input_tokens  >= 0
    assert resp.output_tokens >= 0


# ── AnthropicLLM ─────────────────────────────────────────────────────────────

def test_anthropic_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        AnthropicLLM(api_key=None)


def test_anthropic_accepts_explicit_key():
    with patch("anthropic.AsyncAnthropic"):
        client = AnthropicLLM(api_key="sk-ant-test")
    assert isinstance(client, ILLMClient)


@pytest.mark.asyncio
async def test_anthropic_complete_basic():
    mock_message = MagicMock()
    mock_message.content  = [MagicMock(text="AAPL analysis here")]
    mock_message.model    = "claude-opus-4-8"
    mock_message.usage.input_tokens  = 200
    mock_message.usage.output_tokens = 400

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_cls.return_value.messages.create = AsyncMock(return_value=mock_message)
        client = AnthropicLLM(api_key="sk-ant-test")
        resp   = await client.complete(LLMRequest(prompt="Analyse AAPL"))

    _assert_response(resp, "AAPL analysis here", "claude-opus-4-8")
    assert resp.input_tokens  == 200
    assert resp.output_tokens == 400


@pytest.mark.asyncio
async def test_anthropic_uses_request_model_override():
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="ok")]
    mock_message.model   = "claude-haiku-4-5-20251001"
    mock_message.usage.input_tokens  = 10
    mock_message.usage.output_tokens = 20

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        create = AsyncMock(return_value=mock_message)
        mock_cls.return_value.messages.create = create
        client = AnthropicLLM(api_key="sk-ant-test")
        await client.complete(LLMRequest(prompt="p", model="claude-haiku-4-5-20251001"))

    call_kwargs = create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_anthropic_passes_system_prompt():
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="ok")]
    mock_message.model   = "claude-opus-4-8"
    mock_message.usage.input_tokens  = 10
    mock_message.usage.output_tokens = 10

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        create = AsyncMock(return_value=mock_message)
        mock_cls.return_value.messages.create = create
        client = AnthropicLLM(api_key="sk-ant-test")
        await client.complete(LLMRequest(prompt="p", system="You are an analyst"))

    call_kwargs = create.call_args.kwargs
    assert call_kwargs.get("system") == "You are an analyst"


@pytest.mark.asyncio
async def test_anthropic_no_system_key_when_none():
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="ok")]
    mock_message.model   = "claude-opus-4-8"
    mock_message.usage.input_tokens  = 5
    mock_message.usage.output_tokens = 5

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        create = AsyncMock(return_value=mock_message)
        mock_cls.return_value.messages.create = create
        client = AnthropicLLM(api_key="sk-ant-test")
        await client.complete(LLMRequest(prompt="p"))

    call_kwargs = create.call_args.kwargs
    assert "system" not in call_kwargs


# ── GrokLLM ───────────────────────────────────────────────────────────────────

def test_grok_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        GrokLLM(api_key=None)


@pytest.mark.asyncio
async def test_grok_complete_basic():
    mock_choice          = MagicMock()
    mock_choice.message.content = "Grok AAPL analysis"
    mock_response        = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model   = "grok-3"
    mock_response.usage.prompt_tokens     = 150
    mock_response.usage.completion_tokens = 300

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(return_value=mock_response)
        client = GrokLLM(api_key="xai-test")
        resp   = await client.complete(LLMRequest(prompt="Analyse AAPL"))

    _assert_response(resp, "Grok AAPL analysis", "grok-3")


@pytest.mark.asyncio
async def test_grok_system_prompt_sent_as_system_role():
    mock_choice          = MagicMock()
    mock_choice.message.content = "ok"
    mock_response        = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model   = "grok-3"
    mock_response.usage.prompt_tokens     = 5
    mock_response.usage.completion_tokens = 5

    with patch("openai.AsyncOpenAI") as mock_cls:
        create = AsyncMock(return_value=mock_response)
        mock_cls.return_value.chat.completions.create = create
        client = GrokLLM(api_key="xai-test")
        await client.complete(LLMRequest(prompt="p", system="Be concise"))

    messages = create.call_args.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "Be concise"}
    assert messages[1] == {"role": "user",   "content": "p"}


# ── GeminiLLM ─────────────────────────────────────────────────────────────────

def test_gemini_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        GeminiLLM(api_key=None)


@pytest.mark.asyncio
async def test_gemini_complete_basic():
    mock_response        = MagicMock()
    mock_response.text   = "Gemini AAPL analysis"
    mock_response.usage_metadata.prompt_token_count     = 100
    mock_response.usage_metadata.candidates_token_count = 200

    with patch("google.genai.Client") as mock_cls:
        mock_cls.return_value.aio.models.generate_content = AsyncMock(return_value=mock_response)
        client = GeminiLLM(api_key="AIza-test")
        resp   = await client.complete(LLMRequest(prompt="Analyse AAPL"))

    _assert_response(resp, "Gemini AAPL analysis", "gemini-2.5-flash")
    assert resp.input_tokens  == 100
    assert resp.output_tokens == 200


@pytest.mark.asyncio
async def test_gemini_system_prepended_to_prompt():
    mock_response        = MagicMock()
    mock_response.text   = "ok"
    mock_response.usage_metadata.prompt_token_count     = 5
    mock_response.usage_metadata.candidates_token_count = 5

    with patch("google.genai.Client") as mock_cls:
        generate = AsyncMock(return_value=mock_response)
        mock_cls.return_value.aio.models.generate_content = generate
        client = GeminiLLM(api_key="AIza-test")
        await client.complete(LLMRequest(prompt="my prompt", system="Be an analyst"))

    contents = generate.call_args.kwargs["contents"]
    assert "Be an analyst" in contents
    assert "my prompt"     in contents


@pytest.mark.asyncio
async def test_gemini_uses_request_model_override():
    mock_response        = MagicMock()
    mock_response.text   = "ok"
    mock_response.usage_metadata.prompt_token_count     = 5
    mock_response.usage_metadata.candidates_token_count = 5

    with patch("google.genai.Client") as mock_cls:
        generate = AsyncMock(return_value=mock_response)
        mock_cls.return_value.aio.models.generate_content = generate
        client = GeminiLLM(api_key="AIza-test")
        await client.complete(LLMRequest(prompt="p", model="gemini-2.5-pro"))

    assert generate.call_args.kwargs["model"] == "gemini-2.5-pro"


# ── PerplexityLLM ─────────────────────────────────────────────────────────────

def test_perplexity_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        PerplexityLLM(api_key=None)


@pytest.mark.asyncio
async def test_perplexity_complete_basic():
    mock_choice          = MagicMock()
    mock_choice.message.content = "Perplexity AAPL analysis"
    mock_response        = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model   = "sonar-pro"
    mock_response.usage.prompt_tokens     = 120
    mock_response.usage.completion_tokens = 250

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(return_value=mock_response)
        client = PerplexityLLM(api_key="pplx-test")
        resp   = await client.complete(LLMRequest(prompt="Analyse AAPL"))

    _assert_response(resp, "Perplexity AAPL analysis", "sonar-pro")
    assert resp.input_tokens  == 120
    assert resp.output_tokens == 250


@pytest.mark.asyncio
async def test_perplexity_uses_xai_base_url():
    """Perplexity must point at api.perplexity.ai, not OpenAI."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="ok"))],
                model="sonar-pro",
                usage=MagicMock(prompt_tokens=1, completion_tokens=1),
            )
        )
        PerplexityLLM(api_key="pplx-test")

    _, kwargs = mock_cls.call_args
    assert "perplexity.ai" in kwargs.get("base_url", "")


@pytest.mark.asyncio
async def test_perplexity_system_prompt_as_system_role():
    mock_choice          = MagicMock()
    mock_choice.message.content = "ok"
    mock_response        = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model   = "sonar-pro"
    mock_response.usage.prompt_tokens     = 5
    mock_response.usage.completion_tokens = 5

    with patch("openai.AsyncOpenAI") as mock_cls:
        create = AsyncMock(return_value=mock_response)
        mock_cls.return_value.chat.completions.create = create
        client = PerplexityLLM(api_key="pplx-test")
        await client.complete(LLMRequest(prompt="p", system="Be concise"))

    messages = create.call_args.kwargs["messages"]
    assert messages[0]["role"]    == "system"
    assert messages[0]["content"] == "Be concise"


# ── All clients — interface contract ─────────────────────────────────────────

def test_all_clients_are_illlmclient_subclasses():
    for cls in [AnthropicLLM, GrokLLM, GeminiLLM, PerplexityLLM]:
        assert issubclass(cls, ILLMClient), f"{cls.__name__} must subclass ILLMClient"
