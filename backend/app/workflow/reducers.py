"""State reducers.

Parallel branches must never overwrite each other or the global status, so
map-shaped channels merge per key and list-shaped channels append uniquely.
Only the parent control layer writes the scalar fields (status, passed, counters).
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


def merge_map(current: dict[str, T] | None, update: dict[str, T] | None) -> dict[str, T]:
    """Merge two id-keyed maps; the update wins for each key it provides."""
    merged: dict[str, T] = dict(current or {})
    merged.update(update or {})
    return merged


def append_unique(current: list[T] | None, update: list[T] | None) -> list[T]:
    """Append items that are not already present, preserving order."""
    merged = list(current or [])
    for item in update or []:
        if item not in merged:
            merged.append(item)
    return merged


def extend(current: list[T] | None, update: list[T] | None) -> list[T]:
    return list(current or []) + list(update or [])


def max_int(current: int | None, update: int | None) -> int:
    return max(int(current or 0), int(update or 0))


def last_value(current: Any, update: Any) -> Any:
    return update if update is not None else current
