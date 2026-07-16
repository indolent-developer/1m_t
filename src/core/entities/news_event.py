"""
core.entities.news_event

Event type for the live news pipeline.

Payload for NewsEvent.NEWS_PUBLISHED is a StockNews instance with
fetched_at and news_source populated.
"""
from __future__ import annotations

from enum import Enum


class NewsEvent(str, Enum):
    NEWS_PUBLISHED = "news_published"
