"""
interfaces.telegram.arg_parser

Parse --broker and --account flags from Telegram command text.

Supports:
    /buy AAPL 10
    /buy AAPL 10 --broker ibkr_live
    /buy AAPL 10 --broker ibkr_live --account DU123456
    /buy AAPL 10 --account DU123456 --broker ibkr_live   (order-independent)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ParsedCommand:
    """Result of parsing a Telegram command string."""
    positional: List[str]     # tokens after stripping /command word and flags
    broker:     Optional[str] # --broker value (lowercased), or None
    account:    Optional[str] # --account value (original case), or None
    raw:        str           # original text, for error messages


def parse(text: str) -> ParsedCommand:
    """
    Strip --broker/--account flags and return positional args + flag values.

    Example:
        parse("/buy AAPL 10 --broker ibkr_live --account DU123")
        → ParsedCommand(positional=["AAPL", "10"], broker="ibkr_live", account="DU123")
    """
    broker:  Optional[str] = None
    account: Optional[str] = None

    m = re.search(r"--broker\s+(\S+)", text, re.IGNORECASE)
    if m:
        broker = m.group(1).lower()
        text   = text[:m.start()] + text[m.end():]

    m = re.search(r"--account\s+(\S+)", text, re.IGNORECASE)
    if m:
        account = m.group(1)
        text    = text[:m.start()] + text[m.end():]

    tokens     = text.split()
    positional = [t for t in tokens if not t.startswith("/") and t]

    return ParsedCommand(
        positional=positional,
        broker=broker,
        account=account,
        raw=text.strip(),
    )


def require_args(parsed: ParsedCommand, count: int, usage: str) -> Optional[str]:
    """Return an error string if positional arg count < count, else None."""
    if len(parsed.positional) < count:
        return f"❌ Missing arguments. Usage: `{usage}`"
    return None
