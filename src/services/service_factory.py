"""
services.service_factory

Factory that wires config objects to service constructors.

Usage:
    from core.config.config_loader import config_loader
    from services.service_factory import ServiceFactory

    factory = ServiceFactory(config_loader)

    bus          = factory.event_bus()
    redis        = factory.redis_cache()
    monitor      = factory.price_monitor(symbols=[], bus=bus)
    telegram     = factory.telegram_alert_subscriber(bus=bus, exchange_source=watcher, cache=redis)

Every factory method loads the relevant typed config section, then passes it
directly to the service constructor — no service reads os.environ itself.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from core.config.config_loader import ConfigLoader, config_loader as _default_loader
from core.config.config_models import (
    EventBusConfig,
    FinnhubDataConfig,
    FmpDataConfig,
    IBKRBrokerConfig,
    PriceMonitorConfig,
    RedisConfig,
    TelegramConfig,
)

if TYPE_CHECKING:
    from core.adapters.event_bus import IEventBus
    from infrastructure.cache.redis_cache import RedisCache
    from interfaces.telegram.alert_subscriber import TelegramAlertSubscriber
    from services.price_monitor import PriceMonitor
    from services.symbol_auto_watcher import SymbolAutoWatcher


class ServiceFactory:
    """
    Creates and wires application services from typed config objects.

    Pass a ConfigLoader instance (or omit to use the global singleton).
    Each create_* method loads the minimal config sections it needs.
    """

    def __init__(self, loader: Optional[ConfigLoader] = None) -> None:
        self._loader = loader or _default_loader

    # ── Infrastructure ────────────────────────────────────────────────────────

    def redis_cache(self, cfg: Optional[RedisConfig] = None) -> "RedisCache":
        """Create a RedisCache from redis config."""
        from infrastructure.cache.redis_cache import RedisCache
        cfg = cfg or self._loader.load_redis()
        return RedisCache(url=cfg.url)

    def event_bus(self, cfg: Optional[EventBusConfig] = None) -> "IEventBus":
        """Create LocalEventBus or RedisEventBus based on event_bus.backend config."""
        cfg = cfg or self._loader.load_section("event_bus")
        if cfg.backend == "redis":
            from adapters.events.redis_event_bus import RedisEventBus
            redis_cfg = self._loader.load_redis()
            return RedisEventBus(url=redis_cfg.url, channel_prefix=cfg.redis.channel_prefix)
        from adapters.events.local_event_bus import LocalEventBus
        return LocalEventBus()

    # ── Price feed ────────────────────────────────────────────────────────────

    def price_monitor(
        self,
        symbols: List[str],
        bus: "IEventBus",
        config: Optional[PriceMonitorConfig] = None,
        ibkr_config: Optional[IBKRBrokerConfig] = None,
        finnhub_config: Optional[FinnhubDataConfig] = None,
        fmp_config: Optional[FmpDataConfig] = None,
    ) -> "PriceMonitor":
        """
        Create a PriceMonitor fully wired to its tick sources.

        Config is loaded from services.price_monitor, broker.ibkr, and
        data_apis if not supplied explicitly — callers can override any part.
        """
        from services.price_monitor import PriceMonitor
        config        = config        or self._loader.load_services().price_monitor
        ibkr_config   = ibkr_config   or self._loader.load_broker("ibkr")
        finnhub_config = finnhub_config or self._loader.load_data_apis().finnhub
        fmp_config    = fmp_config    or self._loader.load_data_apis().financialmodelingprep
        return PriceMonitor(
            symbols=symbols,
            bus=bus,
            config=config,
            ibkr_config=ibkr_config,
            finnhub_config=finnhub_config,
            fmp_config=fmp_config,
        )

    # ── Telegram ──────────────────────────────────────────────────────────────

    def telegram_alert_subscriber(
        self,
        bus: "IEventBus",
        exchange_source: "SymbolAutoWatcher",
        cache: "RedisCache",
        cfg: Optional[TelegramConfig] = None,
    ) -> "TelegramAlertSubscriber":
        """Create TelegramAlertSubscriber from telegram config."""
        from interfaces.telegram.alert_subscriber import TelegramAlertSubscriber
        cfg = cfg or self._loader.load_telegram()
        return TelegramAlertSubscriber(
            bus=bus,
            token=cfg.bot_token,
            chat_id=cfg.chat_id,
            chart_id=cfg.chart_id,
            exchange_source=exchange_source,
            cache=cache,
        )
