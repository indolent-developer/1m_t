#!/usr/bin/env python3
"""
scripts.grade_tracker_day — Performance tracker grading helper

Computes the mechanical "predicted vs actual" part of a
data/daily_morning/performance_tracker.md row: for a given date and prompt
stage, reads each model's saved response, fetches the actual EOD close for
every symbol via yfinance, and grades each non-flat `bias` call against the
move vs. `levels.prior_close`.

It does NOT write prose commentary — that's still a judgment call for a
human/LLM pass. Use --write to append skeleton rows (Comment column left
blank) to the tracker table; without it, output goes to stdout only.

Model files are matched by content (run.stage / a known model name found in
the filename), not by a fixed naming scheme, since past folders have used
inconsistent conventions (e.g. `claude1.json` vs `24.06.2026.claude.json`).
A stray ```json fence around the JSON body (seen from some LLM API responses)
is stripped automatically.

Usage:
    python src/scripts/grade_tracker_day.py --date 2026-07-15
    python src/scripts/grade_tracker_day.py --date 2026-07-10 --stage 2
    python src/scripts/grade_tracker_day.py --date 2026-07-15 --write
    python src/scripts/grade_tracker_day.py --date 2026-06-19 --folder 19.06.2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RESPONSES_DIR = _ROOT / "data" / "daily_morning" / "prompt_responses"
_TRACKER_PATH = _ROOT / "data" / "daily_morning" / "performance_tracker.md"

_MODELS = ["chatgpt", "claude", "deepseek", "gemini", "grok"]
_DISPLAY_NAME = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "deepseek": "DeepSeek",
    "gemini": "Gemini",
    "grok": "Grok",
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _load_json_lenient(path: Path) -> dict | None:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stripped = _FENCE_RE.sub("", text).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            print(f"  ! could not parse {path.name} as JSON, skipping", file=sys.stderr)
            return None


def _find_model_for_filename(name: str) -> str | None:
    lname = name.lower()
    for model in _MODELS:
        if model in lname:
            return model
    return None


def _load_stage_responses(folder: Path, stage: int) -> dict[str, dict]:
    """model -> parsed JSON, for every file in folder matching run.stage."""
    found: dict[str, dict] = {}
    for path in sorted(folder.glob("*.json")):
        model = _find_model_for_filename(path.name)
        if model is None:
            print(f"  ! {path.name}: couldn't infer model from filename, skipping", file=sys.stderr)
            continue
        data = _load_json_lenient(path)
        if data is None:
            continue
        if data.get("run", {}).get("stage") != stage:
            continue
        if model in found:
            print(f"  ! multiple stage-{stage} files matched '{model}' in {folder.name}; keeping {path.name}", file=sys.stderr)
        found[model] = data
    return found


def _resolve_folder(date: dt.date, folder_override: str | None) -> Path:
    if folder_override:
        folder = _RESPONSES_DIR / folder_override
        if not folder.is_dir():
            raise SystemExit(f"--folder {folder_override!r} not found under {_RESPONSES_DIR}")
        return folder
    candidate = _RESPONSES_DIR / date.strftime("%m.%d.%Y")
    if candidate.is_dir():
        return candidate
    raise SystemExit(
        f"No folder {candidate.name} under {_RESPONSES_DIR}. "
        f"If this date uses an older DD.MM.YYYY-style folder name, pass --folder explicitly."
    )


def _fetch_actual_closes(symbols: list[str], date: dt.date) -> dict[str, float]:
    import yfinance as yf

    start = date - dt.timedelta(days=7)
    end = date + dt.timedelta(days=1)
    data = yf.download(symbols, start=start, end=end, interval="1d",
                        group_by="ticker", auto_adjust=False, progress=False)
    closes: dict[str, float] = {}
    target = pd_ts(date)
    for sym in symbols:
        try:
            df = data[sym] if len(symbols) > 1 else data
            df = df.dropna()
        except KeyError:
            print(f"  ! no yfinance data for {sym}", file=sys.stderr)
            continue
        if target not in df.index:
            print(f"  ! {sym}: no close on {date.isoformat()} (holiday? delisted? wrong date?)", file=sys.stderr)
            continue
        closes[sym] = float(df.loc[target, "Close"])
    return closes


def pd_ts(date: dt.date):
    import pandas as pd
    return pd.Timestamp(date)


def _market_was_open(date: dt.date) -> bool:
    import yfinance as yf

    df = yf.download("SPY", start=date - dt.timedelta(days=5), end=date + dt.timedelta(days=1),
                      interval="1d", progress=False)
    return pd_ts(date) in df.index


def _grade(bias: str, pct: float) -> bool | None:
    if bias == "long":
        return pct > 0
    if bias == "short":
        return pct < 0
    return None  # flat / other — excluded from the hit-rate denominator


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="Date being graded, YYYY-MM-DD (the EOD move date, matches the tracker row date)")
    ap.add_argument("--stage", type=int, default=1, choices=[1, 2, 3], help="Prompt stage to grade (default: 1)")
    ap.add_argument("--folder", default=None, help="Override the prompt_responses/ subfolder name if it doesn't match MM.DD.YYYY")
    ap.add_argument("--write", action="store_true", help="Append skeleton rows (blank Comment) to performance_tracker.md")
    args = ap.parse_args()

    date = dt.date.fromisoformat(args.date)
    folder = _resolve_folder(date, args.folder)

    print(f"Grading stage {args.stage} for {date.isoformat()} from {folder}\n", file=sys.stderr)
    responses = _load_stage_responses(folder, args.stage)

    # Canonical symbol order: first-seen across models, in file order.
    symbol_order: list[str] = []
    per_model_rows: dict[str, dict[str, tuple[str, float]]] = {}
    for model, data in responses.items():
        rows: dict[str, tuple[str, float]] = {}
        for entry in data.get("universe", []):
            sym = entry["symbol"]
            bias = entry.get("bias", "flat")
            prior_close = entry.get("levels", {}).get("prior_close")
            if prior_close is None:
                continue
            try:
                prior_close = float(prior_close)
            except (TypeError, ValueError):
                print(f"  ! {model}/{sym}: prior_close {prior_close!r} isn't numeric, skipping", file=sys.stderr)
                continue
            rows[sym] = (bias, prior_close)
            if sym not in symbol_order:
                symbol_order.append(sym)
        per_model_rows[model] = rows

    if not symbol_order:
        raise SystemExit("No symbols found in any stage-matched response file — nothing to grade.")

    if not _market_was_open(date):
        raise SystemExit(
            f"{date.isoformat()} looks like a market holiday/weekend (no SPY close found) — "
            f"nothing to grade against. If the response files were generated for the wrong "
            f"session, re-run with the correct --date."
        )

    actual_closes = _fetch_actual_closes(symbol_order, date)

    lines: list[str] = []
    for model in _MODELS:
        rows = per_model_rows.get(model)
        if not rows:
            lines.append(f"| {date.isoformat()} | {_DISPLAY_NAME[model]} | *(no response saved — file empty)* | | | |")
            continue
        parts = []
        hits = 0
        total = 0
        for sym in symbol_order:
            if sym not in rows:
                continue
            bias, prior_close = rows[sym]
            actual = actual_closes.get(sym)
            if actual is None:
                parts.append(f"{sym} {bias} (no EOD data)")
                continue
            pct = (actual - prior_close) / prior_close * 100
            hit = _grade(bias, pct)
            if hit is None:
                parts.append(f"{sym} {bias}")
            else:
                total += 1
                hits += 1 if hit else 0
                parts.append(f"{sym} {bias}{'✓' if hit else '✗'}")
        pct_score = f"{round(100 * hits / total)}%" if total else "n/a"
        summary = f"**{hits}/{total} ({pct_score})**" if total else "**0/0 (n/a)**"
        row_text = " · ".join(parts) + f" → {summary}"
        lines.append(f"| {date.isoformat()} | {_DISPLAY_NAME[model]} | {row_text} | | | |")

    print("\n".join(lines))

    if args.write:
        _write_rows(lines)


def _write_rows(lines: list[str]) -> None:
    text = _TRACKER_PATH.read_text()
    marker = "\n---\n\n## Notes"
    idx = text.find(marker)
    if idx == -1:
        raise SystemExit("Could not find the table/notes boundary in performance_tracker.md — append manually.")
    insertion = "\n".join(lines) + "\n"
    new_text = text[:idx] + "\n" + insertion + text[idx + 1:]
    _TRACKER_PATH.write_text(new_text)
    print(f"\nAppended {len(lines)} row(s) to {_TRACKER_PATH} (Comment column left blank).", file=sys.stderr)


if __name__ == "__main__":
    main()
