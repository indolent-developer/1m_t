"""
core.utils.log_helper

Structured contextual logging for the trading system.

Design principles:
    - getLogger() is safe to call multiple times for the same name —
      handlers are added exactly once per logger (no duplicate lines)
    - Global setup (file handler, config) happens once, lazily, thread-safely
    - Context variables propagate automatically across async tasks and threads
    - log level hierarchy: env var > set_global_loglevel() > per-logger default

Context (set once at startup, appears on every log line):

    # Set multiple keys at once — preferred
    set_log_context({"symbol": "AAPL", "broker": "capital", "strategy": "supertrend"})

    # Or use individual shorthands
    set_log_symbol("AAPL")
    set_log_broker("capital")

    # Keys and their log-line rendering:
    #   symbol            → [AAPL]
    #   strategy          → [strat:supertrend]
    #   strategy_version  → [v:2.1]
    #   broker            → [brk:capital]
    #   run_id            → [run:abc123]
    #   order_id          → [ord:ord456]
    #   app_name          → ron2  (shown without brackets)

    # Clear one key
    set_log_context({"order_id": ""})

    # Clear all context
    clear_log_context()

Usage:
    from core.utils.log_helper import getLogger, set_log_context

    logger = getLogger(__name__)
    set_log_context({"symbol": "AAPL", "broker": "capital"})
    logger.info("Order placed")
    # → 2026-06-04 09:32:01 INFO  ron2-[AAPL] [brk:capital] order.py-42 - Order placed

Global log level override (e.g. from CLI flag --debug):
    from core.utils.log_helper import set_global_loglevel
    import logging
    set_global_loglevel(logging.DEBUG)
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, Union


# ── Log context keys ──────────────────────────────────────────────────────────

class LogKey(str, Enum):
    """
    Valid keys for set_log_context().

    Using LogKey prevents typos at call sites — the IDE autocompletes and
    passing an unknown string raises a KeyError immediately instead of
    silently not appearing in logs.

    Log-line rendering:
        SYMBOL            → [AAPL]
        STRATEGY          → [strat:supertrend]
        STRATEGY_VERSION  → [v:2.1]
        BROKER            → [brk:capital]
        RUN_ID            → [run:abc123]
        ORDER_ID          → [ord:ord456]
        APP_NAME          → ron2  (no brackets, always shown)
    """
    SYMBOL           = "symbol"
    APP_NAME         = "app_name"
    STRATEGY         = "strategy"
    STRATEGY_VERSION = "strategy_version"
    BROKER           = "broker"
    RUN_ID           = "run_id"
    ORDER_ID         = "order_id"


# Alias for callers that prefer shorter imports:
#   from core.utils.log_helper import LK
#   set_log_context({LK.SYMBOL: "AAPL"})
LK = LogKey

# Internal — derived from enum, never needs manual maintenance
_KNOWN_KEYS: frozenset = frozenset(k.value for k in LogKey)

# Type accepted by set_log_context — either LogKey enum or raw string
_ContextFields = Dict[Union[LogKey, str], str]

_DEFAULT_CONTEXT: Dict[str, str] = {LogKey.APP_NAME.value: "ron2"}

_ctx: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "log_context", default=_DEFAULT_CONTEXT
)


# ── Public context API ────────────────────────────────────────────────────────

def set_log_context(fields: _ContextFields) -> None:
    """
    Set one or more log context fields.

    Keys must be LogKey enum values (or their string equivalents for
    backwards compatibility). Unknown keys raise KeyError immediately.

    Merges into the current context — only the keys you provide are updated.

    Example:
        from core.utils.log_helper import set_log_context, LogKey

        set_log_context({LogKey.SYMBOL: "AAPL", LogKey.BROKER: "capital"})
        set_log_context({LogKey.ORDER_ID: "ord123"})
        set_log_context({LogKey.ORDER_ID: ""})   # clear a single key

        # Short alias
        from core.utils.log_helper import LK
        set_log_context({LK.SYMBOL: "NVDA", LK.STRATEGY: "supertrend"})
    """
    current = dict(_ctx.get())
    for k, v in fields.items():
        # Normalise: accept LogKey enum or raw string
        key = k.value if isinstance(k, LogKey) else str(k)
        if key not in _KNOWN_KEYS:
            raise KeyError(
                f"Unknown log context key: {k!r}. "
                f"Use LogKey enum values: {[e.value for e in LogKey]}"
            )
        current[key] = str(v)
    _ctx.set(current)


def get_log_context() -> Dict[str, str]:
    """Return a copy of the current log context dict."""
    return dict(_ctx.get())


def clear_log_context() -> None:
    """Reset all context fields to defaults (keeps APP_NAME)."""
    _ctx.set(dict(_DEFAULT_CONTEXT))


# ── Individual shorthands (backwards compatible) ──────────────────────────────
# These delegate to set_log_context — existing call sites work unchanged.

def set_log_symbol(symbol: str)            -> None: set_log_context({LogKey.SYMBOL: symbol})
def set_log_app_name(app_name: str)        -> None: set_log_context({LogKey.APP_NAME: app_name})
def set_log_strategy(strategy: str)        -> None: set_log_context({LogKey.STRATEGY: strategy})
def set_log_strategy_version(v: str)       -> None: set_log_context({LogKey.STRATEGY_VERSION: v})
def set_log_broker(broker: str)            -> None: set_log_context({LogKey.BROKER: broker})
def set_log_run_id(run_id: str)            -> None: set_log_context({LogKey.RUN_ID: run_id})
def set_log_order_id(order_id: str)        -> None: set_log_context({LogKey.ORDER_ID: order_id})


# ── Context filter ────────────────────────────────────────────────────────────

class _ContextFilter(logging.Filter):
    """
    Injects log context dict into every LogRecord.

    Reads from the single _ctx ContextVar and adds formatted attributes
    for use in format strings:
        %(symbol)s            "[AAPL] "        or ""
        %(app_name)s          "ron2"
        %(strategy)s          "[strat:name] "  or ""
        %(strategy_version)s  "[v:1.0] "       or ""
        %(broker)s            "[brk:capital] " or ""
        %(run_id)s            "[run:abc] "      or ""
        %(order_id)s          "[ord:xyz] "      or ""

    Also populates record.json_fields for structured log sinks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _ctx.get()

        sym = ctx.get("symbol", "")
        record.symbol   = f"[{sym}] " if sym else ""
        record.app_name = ctx.get("app_name", "ron2")

        strat = ctx.get("strategy", "")
        record.strategy = f"[strat:{strat}] " if strat else ""

        ver = ctx.get("strategy_version", "")
        record.strategy_version = f"[v:{ver}] " if ver else ""

        brk = ctx.get("broker", "")
        record.broker = f"[brk:{brk}] " if brk else ""

        run = ctx.get("run_id", "")
        record.run_id = f"[run:{run}] " if run else ""

        oid = ctx.get("order_id", "")
        record.order_id = f"[ord:{oid}] " if oid else ""

        # Structured fields for any future JSON log sink
        record.json_fields = {k: v for k, v in ctx.items() if v}

        return True


# ── Format strings ────────────────────────────────────────────────────────────

_CONSOLE_FMT = (
    "%(asctime)s-%(levelname)-6s-%(app_name)s-"
    "%(symbol)s%(strategy)s%(strategy_version)s%(broker)s%(run_id)s%(order_id)s"
    "%(filename)s-%(lineno)d - %(message)s"
)
_FILE_FMT = (
    "%(asctime)s-%(levelname)-6s-%(app_name)s-"
    "%(symbol)s%(strategy)s%(strategy_version)s%(broker)s%(run_id)s%(order_id)s"
    "%(filename)s-%(lineno)d-%(process)d - %(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ── Global state (initialised once, thread-safe) ──────────────────────────────

_lock              = threading.Lock()
_context_filter    = _ContextFilter()
_file_handler:      Optional[logging.Handler] = None
_logging_config:    Optional[dict] = None
_global_level:      Optional[int]  = None

# Honour LOG_LEVEL env var set by launch scripts
_env_level = os.environ.get("LOG_LEVEL", "").upper()
if _env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _global_level = getattr(logging, _env_level)


def _load_config() -> dict:
    """
    Load logging config from ConfigLoader exactly once.
    Falls back to {} on any error (avoids circular import failures at startup).
    """
    global _logging_config
    if _logging_config is not None:
        return _logging_config
    try:
        from core.config.config_loader import config_loader
        _logging_config = config_loader.get("logging") or {}
    except Exception:
        _logging_config = {}
    return _logging_config


def _get_log_dir() -> Path:
    cfg     = _load_config()
    log_dir = Path(cfg.get("dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _build_file_handler(
    log_dir:    Path,
    prefix:     str,
    level:      int,
    max_bytes:  int,
    backup_count: int,
) -> logging.Handler:
    """Create a RotatingFileHandler for today's log file."""
    filename = log_dir / f"{prefix}_{datetime.now().strftime('%Y-%m-%d')}.log"
    handler  = RotatingFileHandler(
        filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    handler.addFilter(_context_filter)
    return handler


def _resolve_level(cfg: dict, key: str, fallback: int) -> int:
    """Parse a string level name from config (e.g. 'DEBUG') into a logging int."""
    raw = cfg.get(key, "")
    if isinstance(raw, str) and raw.upper() in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        return getattr(logging, raw.upper())
    return fallback


def _ensure_file_handler() -> logging.Handler:
    """
    Return the global file handler, creating it on first call.
    Level is read from config key file_level (default DEBUG).
    Thread-safe: uses module-level lock.
    """
    global _file_handler
    if _file_handler is not None:
        return _file_handler

    with _lock:
        if _file_handler is not None:          # double-checked locking
            return _file_handler

        cfg          = _load_config()
        prefix       = cfg.get("file_prefix",   "ron2")
        max_bytes    = cfg.get("max_bytes",      10 * 1024 * 1024)  # 10 MB
        backup_count = cfg.get("backup_count",   5)
        file_level   = _resolve_level(cfg, "file_level", logging.DEBUG)
        log_dir      = _get_log_dir()

        _file_handler = _build_file_handler(
            log_dir, prefix, file_level, max_bytes, backup_count
        )

    return _file_handler


# ── Public API ────────────────────────────────────────────────────────────────

def getLogger(
    name:     str,
    level:    int = logging.INFO,
    app_name: str = "ron2",
) -> logging.Logger:
    """
    Get or create a logger.

    Safe to call multiple times for the same name — handlers are added
    exactly once. Subsequent calls return the cached logger unchanged.

    Console level and file level are read from the logging config section
    (console_level / file_level). The `level` parameter is used as the
    console fallback when no config is present.

    Parameters
    ----------
    name:       Module name, typically __name__.
    level:      Fallback console level when config is absent.
    app_name:   Appears in every log line as the app identifier.
    """
    if app_name:
        set_log_context({LogKey.APP_NAME: app_name})

    cfg = _load_config()

    # Resolve per-handler levels from config; --debug flag overrides both
    if _global_level is not None:
        console_level = _global_level
        file_level    = _global_level
    else:
        console_level = _resolve_level(cfg, "console_level", level)
        file_level    = _resolve_level(cfg, "file_level",    logging.DEBUG)

    # quiet_modules silences noisy internal packages at WARNING+
    quiet = cfg.get("quiet_modules", [])
    if any(name.startswith(m) for m in quiet):
        console_level = logging.WARNING
        file_level    = logging.WARNING

    logger = logging.getLogger(name)

    # Guard: only configure once per logger
    if logger.handlers:
        return logger

    # Logger itself must pass the lowest of the two levels so both handlers
    # receive the records they need.
    logger.setLevel(min(console_level, file_level))
    logger.propagate = False

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    console.addFilter(_context_filter)
    logger.addHandler(console)

    # ── File handler (shared across all loggers) ──────────────────────────────
    file_handler = _ensure_file_handler()
    logger.addHandler(file_handler)

    return logger


def set_global_loglevel(level: int) -> None:
    """
    Set log level for all existing and future loggers.

    Call this early in startup when --debug flag is set:
        set_global_loglevel(logging.DEBUG)

    Affects:
        - All already-created loggers and their handlers
        - The root logger (catches third-party libraries)
        - Future getLogger() calls
    """
    global _global_level
    _global_level = level

    # Root logger — catches libraries that use logging.getLogger() with no name
    logging.getLogger().setLevel(level)

    # All named loggers already created
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            logger.setLevel(level)
            for handler in logger.handlers:
                handler.setLevel(level)

    # Update global file handler if already created
    if _file_handler is not None:
        _file_handler.setLevel(level)


def redirect_to_file(full_name: str, level: Optional[int] = None) -> None:
    """
    Redirect ALL loggers to a specific named log file.

    Use this when launching a dedicated runner (e.g. a backtest for AAPL/supertrend)
    that should write its output to its own file rather than the shared daily log.

    Parameters
    ----------
    full_name:  Log file name (with or without .log extension).
    level:      Log level for the new file. Defaults to current global level.
    """
    global _file_handler

    effective_level = level or _global_level or logging.INFO
    log_dir         = _get_log_dir()
    cfg             = _load_config()
    max_bytes       = cfg.get("max_bytes",    10 * 1024 * 1024)
    backup_count    = cfg.get("backup_count", 5)

    fname = full_name if full_name.endswith(".log") else f"{full_name}.log"

    with _lock:
        old_handler = _file_handler
        new_handler = _build_file_handler(
            log_dir, fname.removesuffix(".log"),
            effective_level, max_bytes, backup_count,
        )
        # Override filename to use the full name directly
        new_handler.baseFilename = str(log_dir / fname)

        _file_handler = new_handler

        # Migrate all existing loggers to the new file
        for logger in logging.Logger.manager.loggerDict.values():
            if not isinstance(logger, logging.Logger):
                continue
            if old_handler and old_handler in logger.handlers:
                logger.removeHandler(old_handler)
            if new_handler not in logger.handlers:
                logger.addHandler(new_handler)

        if old_handler:
            try:
                old_handler.close()
            except Exception:
                pass


def reset() -> None:
    """
    Reset all logging state. Intended for tests only.
    Removes handlers from all loggers and clears global state.
    """
    global _file_handler, _logging_config, _global_level

    with _lock:
        for logger in logging.Logger.manager.loggerDict.values():
            if isinstance(logger, logging.Logger):
                logger.handlers.clear()

        if _file_handler:
            try:
                _file_handler.close()
            except Exception:
                pass

        _file_handler   = None
        _logging_config = None
        _global_level   = None
