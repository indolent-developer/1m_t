"""
infrastructure.gateways.market_data.finnhub_client — Finnhub

Company news and sentiment via Finnhub REST API.

Setup:
    export FINNHUB_API_KEY="your_key_here"
    # or add FINNHUB_API_KEY=... to .env
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

import requests

from core.entities.market_data import StockNews

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubClient:

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or os.environ.get("FINNHUB_API_KEY")
        if not self._key:
            raise ValueError("Finnhub API key required — set FINNHUB_API_KEY env var or pass api_key")

    def _get(self, endpoint: str, **params) -> list | dict:
        params["token"] = self._key
        resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_company_news(
        self,
        ticker: str,
        from_date: date,
        to_date:   date,
    ) -> list[StockNews]:
        """Fetch company news within a date range."""
        raw = self._get(
            "company-news",
            symbol=ticker,
            **{"from": from_date.isoformat(), "to": to_date.isoformat()},
        )
        if not isinstance(raw, list):
            return []

        news = []
        for item in raw:
            try:
                ts = item.get("datetime", 0)
                published = datetime.fromtimestamp(ts) if ts else datetime.utcnow()
                news.append(StockNews(
                    symbol=ticker,
                    published_date=published,
                    publisher=item.get("source", ""),
                    title=item.get("headline", ""),
                    url=item.get("url", ""),
                    text=item.get("summary", ""),
                    image=item.get("image", ""),
                    site=item.get("source", ""),
                ))
            except Exception:
                pass
        return news

    def get_news_sentiment(self, ticker: str) -> dict:
        """Fetch aggregated news sentiment score for a ticker."""
        return self._get("news-sentiment", symbol=ticker)

    def get_basic_financials(self, ticker: str) -> dict:
        """Fetch key metrics snapshot."""
        return self._get("stock/metric", symbol=ticker, metric="all")
