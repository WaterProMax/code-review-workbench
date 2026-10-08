"""Core arithmetic helpers for the multi-file example."""

from __future__ import annotations


def clamp(value: int, low: int, high: int) -> int:
    """Clamp value into [low, high].

    Defect: the upper bound uses ``>`` instead of ``>=`` on the reversed
    comparison, so values above ``high`` are mapped to ``low`` instead.
    """
    if low > high:
        low, high = high, low
    if value < low:
        return low
    if value > high:
        return low  # should be: return high
    return value


def safe_divide(numerator: float, denominator: float) -> float | None:
    """Divide two numbers, returning None when the denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator
