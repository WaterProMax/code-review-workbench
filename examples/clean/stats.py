"""Clean example: small statistics helpers with no known defects.

Used by acceptance scenario A01 (first review passes, fix/verify are skipped).
"""

from __future__ import annotations


def mean(values: list[float]) -> float:
    """Arithmetic mean; rejects an empty input instead of dividing by zero."""
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """Median value; averages the two middle elements for an even count."""
    if not values:
        raise ValueError("median() requires at least one value")
    ordered = sorted(values)
    size = len(ordered)
    middle = size // 2
    if size % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def running_total(values: list[float]) -> list[float]:
    """Cumulative sums, built from a fresh list each call."""
    total = 0.0
    result: list[float] = []
    for value in values:
        total += value
        result.append(total)
    return result
