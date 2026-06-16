"""
infrastructure.gateways.market_data.fmp_client — Financial Modeling Prep

News and fundamental data via FMP REST API.

Setup:
    export FMP_API_KEY="your_key_here"
    # or add FMP_API_KEY=... to .env
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

import requests

from core.entities.analyst_data import Grade, GradeType
from core.entities.market_data import StockNews

BASE_URL = "https://financialmodelingprep.com/api/v3"


class FMPClient:

    def __init__(self, api_key: str | None = None) -> None:
        self._key = api_key or os.environ.get("FMP_API_KEY")
        if not self._key:
            raise ValueError("FMP API key required — set FMP_API_KEY env var or pass api_key")

    def _get(self, endpoint: str, **params) -> list | dict:
        params["apikey"] = self._key
        resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_stock_news(
        self,
        ticker: str,
        limit: int = 20,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> list[StockNews]:
        """Fetch recent news articles for a ticker."""
        params: dict = {"tickers": ticker, "limit": limit}
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()

        raw = self._get("stock_news", **params)
        if not isinstance(raw, list):
            return []

        news = []
        for item in raw:
            try:
                news.append(StockNews.from_dict({
                    "symbol":        item.get("symbol", ticker),
                    "publishedDate": item.get("publishedDate", ""),
                    "publisher":     item.get("publisher", ""),
                    "title":         item.get("title", ""),
                    "url":           item.get("url", ""),
                    "text":          item.get("text", ""),
                    "image":         item.get("image", ""),
                    "site":          item.get("site", ""),
                }))
            except Exception:
                pass
        return news

    def get_press_releases(self, ticker: str, limit: int = 10) -> list[dict]:
        """Fetch SEC filings / press releases."""
        raw = self._get("press-releases", symbol=ticker, limit=limit)
        return raw if isinstance(raw, list) else []

    def get_analyst_grades(self, ticker: str, limit: int = 10) -> list[Grade]:
        """Fetch analyst rating changes."""
        raw = self._get("grade", symbol=ticker, limit=limit)
        if not isinstance(raw, list):
            return []
        grades = []
        for item in raw:
            try:
                grades.append(Grade.from_dict(item))
            except Exception:
                pass
        return grades
