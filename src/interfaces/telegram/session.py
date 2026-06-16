"""
interfaces.telegram.session

Per-chat broker + account context.

Resolution order for every command:
    1. --broker / --account flags in the command string  (highest priority)
    2. SessionContext.active_broker / active_account     (/use command)
    3. Config default passed to SessionManager           (lowest priority)

Broker names match the keys in the TradingBot.brokers registry, e.g.:
    "capital_live", "capital_demo", "ibkr_live", "ibkr_demo", "etoro"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional


@dataclass
class SessionContext:
    """
    Active broker + account for one Telegram chat session.
    Set via /use; overridden per-command with --broker / --account.
    """
    chat_id:        int
    active_broker:  str = ""      # e.g. "capital_live", "ibkr_demo", "etoro"
    active_account: str = ""      # e.g. "DU123456"; empty = broker default
    set_at:         Optional[datetime] = None

    def set(self, broker: str, account: str = "") -> None:
        self.active_broker  = broker.lower().strip()
        self.active_account = account.strip()
        self.set_at         = datetime.now(timezone.utc)

    def clear(self) -> None:
        self.active_broker  = ""
        self.active_account = ""
        self.set_at         = None

    def describe(self) -> str:
        if not self.active_broker:
            return "No broker set — using config default"
        acct  = f" / account: {self.active_account}" if self.active_account else ""
        since = f"  (set {self.set_at.strftime('%H:%M UTC')})" if self.set_at else ""
        return f"Broker: {self.active_broker}{acct}{since}"


class SessionManager:
    """
    One SessionContext per chat_id.
    Single-user bot in practice, but the dict supports multi-user expansion.
    """

    def __init__(self, default_broker: str = "") -> None:
        self._sessions:       Dict[int, SessionContext] = {}
        self._default_broker: str = default_broker

    def get(self, chat_id: int) -> SessionContext:
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionContext(
                chat_id=chat_id,
                active_broker=self._default_broker,
            )
        return self._sessions[chat_id]

    def resolve(
        self,
        chat_id:        int,
        flag_broker:    Optional[str] = None,
        flag_account:   Optional[str] = None,
        config_default: str = "capital",
    ) -> tuple[str, str]:
        """
        Return (broker_name, account_id) by priority:
            1. command --broker / --account flags
            2. session active_broker / active_account
            3. config_default
        """
        ctx     = self.get(chat_id)
        broker  = (flag_broker  or ctx.active_broker  or config_default).lower()
        account = flag_account  or ctx.active_account or ""
        return broker, account
