#!/usr/bin/env python3
"""
src/automation/run_ai_analysis.py

Submit a stock analysis prompt to Claude or Grok via their APIs.
No browser automation — direct API calls, no bot detection.

Setup:
  export ANTHROPIC_API_KEY="sk-ant-..."
  export XAI_API_KEY="xai-..."          # for --target grok

Run:
  .penv/bin/python src/automation/run_ai_analysis.py --tickers DELL HPE SPCE
  .penv/bin/python src/automation/run_ai_analysis.py --tickers AAPL --target grok
  .penv/bin/python src/automation/run_ai_analysis.py --tickers NVDA --save

Pipe from scanner:
  .penv/bin/python src/scripts/run_post_market_scanner.py | \\
      .penv/bin/python src/automation/run_ai_analysis.py --stdin --save
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.adapters.llm import ILLMClient, LLMRequest
from infrastructure.gateways.llms.anthropic_client import AnthropicLLM
from infrastructure.gateways.llms.gemini_client import GeminiLLM
from infrastructure.gateways.llms.grok_client import GrokLLM
from infrastructure.gateways.llms.perplexity_client import PerplexityLLM

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT        = Path(__file__).resolve().parents[2]
_PROMPT_FILE = _ROOT / "prompts" / "stock_mover_analysis.txt"
_OUT_DIR     = _ROOT / "outputs" / "ai_analysis"

# ── Prompt helpers ────────────────────────────────────────────────────────────

def load_prompt(tickers: list[str]) -> str:
    if not _PROMPT_FILE.exists():
        sys.exit(f"Prompt file not found: {_PROMPT_FILE}")
    template = _PROMPT_FILE.read_text()
    return template.replace("[Insert List of Stocks / Tickers Here]", ", ".join(tickers))


def tickers_from_stdin() -> list[str]:
    """Extract ticker symbols from piped scanner output."""
    text = sys.stdin.read()
    found = re.findall(r'\b([A-Z]{2,5})\b', text)
    _SKIP = {"US", "USD", "PM", "REG", "CHG", "VOL", "AVG", "ETF", "IPO",
             "UP", "DOWN", "MKTCAP", "SECTOR", "NAME"}
    seen, result = set(), []
    for t in found:
        if t not in seen and t not in _SKIP:
            seen.add(t)
            result.append(t)
    return result


# ── Core runner ───────────────────────────────────────────────────────────────

async def run(client: ILLMClient, prompt: str) -> str:
    request  = LLMRequest(prompt=prompt)
    response = await client.complete(request)
    print(f"  Tokens: {response.input_tokens} in / {response.output_tokens} out"
          f" / {response.total_tokens} total  |  model: {response.model}")
    return response.text


# ── Output ────────────────────────────────────────────────────────────────────

def print_response(text: str) -> None:
    print(f"\n{'─' * 70}")
    print(text)
    print(f"{'─' * 70}\n")


def save_response(target: str, tickers: list[str], text: str) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts    = datetime.now().strftime("%Y%m%d_%H%M")
    fname = _OUT_DIR / f"{target}_{'_'.join(tickers[:5])}_{ts}.txt"
    fname.write_text(text)
    print(f"  Saved → {fname}")


# ── Client factory ────────────────────────────────────────────────────────────

def build_client(target: str) -> ILLMClient:
    if target == "claude":
        return AnthropicLLM()
    if target == "grok":
        return GrokLLM()
    if target == "gemini":
        return GeminiLLM()
    if target == "perplexity":
        return PerplexityLLM()
    raise ValueError(f"Unknown target: {target}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run stock analysis prompt via Claude or Grok API"
    )
    parser.add_argument("--target",  choices=["claude", "grok", "gemini", "perplexity"],
                        default="claude", help="AI target (default: claude)")
    parser.add_argument("--tickers", nargs="+", metavar="TICKER",
                        help="Stock tickers to analyse")
    parser.add_argument("--stdin",   action="store_true",
                        help="Read tickers from piped scanner output")
    parser.add_argument("--save",    action="store_true",
                        help="Save response to outputs/ai_analysis/")
    args = parser.parse_args()

    if args.stdin:
        tickers = tickers_from_stdin()
        if not tickers:
            sys.exit("No tickers found in stdin.")
        print(f"  Tickers from scanner: {', '.join(tickers)}")
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        parser.error("Provide --tickers AAPL MSFT … or --stdin")

    client = build_client(args.target)
    prompt = load_prompt(tickers)

    print(f"\n  Target : {args.target.capitalize()}")
    print(f"  Tickers: {', '.join(tickers)}")

    text = asyncio.run(run(client, prompt))

    print_response(text)

    if args.save:
        save_response(args.target, tickers, text)


if __name__ == "__main__":
    main()
