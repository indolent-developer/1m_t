"""
core.utils.mapping

Helpers for mapping API response dicts to typed entity objects.
"""
from __future__ import annotations

from typing import Any, Type, TypeVar

T = TypeVar("T")


def _rename(d: dict, mapping: dict[str, str]) -> dict:
    """Return a copy of d with keys renamed according to mapping."""
    result = {}
    for k, v in d.items():
        result[mapping.get(k, k)] = v
    return result


def dict_to_obj(d: dict, cls: Type[T], mapping: dict[str, str] | None = None) -> T:
    """
    Convert a single dict to an instance of cls.

    Priority:
      1. cls.from_dict(d)  — if the class defines a factory classmethod
      2. cls(**renamed_d)  — for plain dataclasses
    """
    if mapping:
        d = _rename(d, mapping)
    if hasattr(cls, "from_dict"):
        return cls.from_dict(d)
    return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__}) \
        if hasattr(cls, "__dataclass_fields__") else cls(**d)


def dict_arr_to_obj_arr(
    items: list[dict] | None,
    cls: Type[T],
    mapping: dict[str, str] | None = None,
) -> list[T]:
    """Convert a list of dicts to a list of cls instances."""
    if not items:
        return []
    result = []
    for item in items:
        try:
            result.append(dict_to_obj(item, cls, mapping))
        except Exception:
            pass
    return result


def dict_arr_to_obj_arr_dict_in(
    items: list[dict] | None,
    cls: Type[T],
    mapping: dict[str, str] | None = None,
) -> list[T]:
    """
    Same as dict_arr_to_obj_arr but handles FMP-style responses where
    the payload is wrapped in a nested dict (e.g. profile endpoint).
    Falls back to dict_arr_to_obj_arr if items are already flat.
    """
    return dict_arr_to_obj_arr(items, cls, mapping)


def read_dict_key(
    key: str,
    d: dict,
    default: Any = None,
    required: bool = False,
) -> Any:
    """Read a key from a dict, with optional default and required check."""
    if key not in d:
        if required:
            raise ValueError(f"Required config key '{key}' is missing")
        return default
    return d[key]
