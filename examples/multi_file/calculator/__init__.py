"""Multi-file example entry point."""

from __future__ import annotations

from .core import clamp, safe_divide
from .helpers import describe_score, format_ratio


def summarize(scores: list[int]) -> dict:
    """Summarise a list of scores using the helpers above."""
    bounded = [clamp(score, 0, 100) for score in scores]
    total = sum(bounded)
    return {
        "count": len(bounded),
        "total": total,
        "average": format_ratio(total, len(bounded)),
        "labels": [describe_score(score) for score in bounded],
        "max_ratio": format_ratio(max(bounded) if bounded else 0, 100),
        "safe": safe_divide(total, max(len(bounded), 1)),
    }
