"""
core.entities.base_entity

Lightweight base for API-mapped entities.
Prefer concrete dataclasses (see new-entities pattern) over this for all new
entities.  Keep this for legacy adapters that need dynamic field mapping.
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, Optional


class BaseEntity:
    """Base class for dynamically-mapped API response entities.

    Prefer typed @dataclass subclasses for new entities.
    Use this only when field names are not known at design time.
    """

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        field_mapping: Dict[str, str] | None = None,
        transformations: Dict[str, Callable[[Any], Any]] | None = None,
    ) -> "BaseEntity":
        """Map an API response dict to an entity instance.

        Args:
            data:             Raw API response dictionary.
            field_mapping:    {api_key: entity_attr} rename map.
            transformations:  {entity_attr: callable} value transforms.

        Returns:
            Entity instance with mapped and transformed attributes.
        """
        field_mapping = field_mapping or {}
        transformations = transformations or {}
        mapped: Dict[str, Any] = {}

        for api_key, value in data.items():
            attr = field_mapping.get(api_key, api_key)
            if attr in transformations:
                value = transformations[attr](value)
            mapped[attr] = value

        return cls(**mapped)


# ── Shared transformation helpers ─────────────────────────────────────────────

def parse_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime.datetime]:
    """Parse a date-string into a datetime; returns None on failure."""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.strptime(str(value), fmt)
    except ValueError:
        return None


def parse_date(value: Any, fmt: str = "%Y-%m-%d") -> Optional[datetime.date]:
    """Parse a date-string into a date; returns None on failure."""
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.datetime.strptime(str(value), fmt).date()
    except ValueError:
        return None


def to_float(value: Any, default: float = 0.0) -> float:
    """Coerce value to float; returns *default* on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    """Coerce value to int; returns *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
