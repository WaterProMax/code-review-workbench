"""String/formatting helpers for the multi-file example."""

from __future__ import annotations

from .core import clamp, safe_divide


def format_ratio(numerator: float, denominator: float) -> str:
    """Return a human readable ratio, or 'n/a' when undefined."""
    value = safe_divide(numerator, denominator)
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def describe_score(score: int) -> str:
    """Map a 0-100 score onto a label, clamped to the valid range."""
    bounded = clamp(score, 0, 100)
    if bounded >= 90:
        return "excellent"
    if bounded >= 60:
        return "pass"
    return "fail"
