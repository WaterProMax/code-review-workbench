"""Repairable example: helpers with concrete, fixable defects.

Used by acceptance scenarios A02/A03/A27 (a patch is produced, applied and
verified; a minimal reproduction test fails before the fix and passes after).
"""

from __future__ import annotations


def add_item(item, items=[]):  # B006: mutable default argument shared across calls
    """Append item to a list and return it (defect: shared default list)."""
    items.append(item)
    return items


def average(values):
    """Arithmetic mean (defect: raises ZeroDivisionError on an empty list)."""
    return sum(values) / len(values)


def parse_port(text):
    """Convert text to a port number (defect: no validation, returns 0 for '')."""
    try:
        return int(text)
    except ValueError:
        return 0
