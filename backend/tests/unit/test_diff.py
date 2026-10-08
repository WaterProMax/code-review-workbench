"""P02: strict patch application semantics (§6.3, acceptance A07/A27/A28)."""

from __future__ import annotations

import pytest

from app.schemas.artifacts import StructuredEdit
from app.services.workspace import (
    DiffError,
    apply_hunks,
    apply_structured_edits,
    apply_unified_diff,
    is_protected_test_path,
    parse_unified_diff,
)

ORIGINAL = """def add_item(item, items=[]):
    items.append(item)
    return items


def average(values):
    return sum(values) / len(values)
"""

FIX_DIFF = """--- a/helpers.py
+++ b/helpers.py
@@ -1,3 +1,5 @@
-def add_item(item, items=[]):
+def add_item(item, items=None):
+    if items is None:
+        items = []
     items.append(item)
     return items


def average(values):
"""


def test_unified_diff_rewrites_the_target_block() -> None:
    result = apply_unified_diff(ORIGINAL, FIX_DIFF)
    assert "def add_item(item, items=None):" in result
    assert "        items = []" in result
    assert "items=[]" not in result
    assert result.endswith("\n")


def test_diff_is_applied_by_strict_equality_not_fuzzy() -> None:
    with pytest.raises(DiffError):
        apply_unified_diff(ORIGINAL.replace("    items.append(item)", "    items.append(item)  "), FIX_DIFF)


def test_hunk_line_counts_are_enforced() -> None:
    truncated = "--- a/x.py\n+++ b/x.py\n@@ -1,5 +1,7 @@\n-def add_item(item, items=[]):\n"
    with pytest.raises(DiffError):
        parse_unified_diff(truncated)


def test_malformed_diff_is_rejected() -> None:
    with pytest.raises(DiffError):
        parse_unified_diff("not a diff at all")
    with pytest.raises(DiffError):
        parse_unified_diff("--- a/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n")


def test_multi_hunk_and_offset_tolerance() -> None:
    original = "a\nb\nc\nd\ne\nf\n"
    diff = """--- a/x.py
+++ b/x.py
@@ -2,2 +2,2 @@
-b
+B
 c
@@ -5,2 +5,2 @@
-e
+E
 f
"""
    assert apply_unified_diff(original, diff) == "a\nB\nc\nd\nE\nf\n"


def test_file_without_trailing_newline_stays_without_one() -> None:
    original = "x = 1\ny = 2"
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 10\n y = 2\n"
    assert apply_unified_diff(original, diff) == "x = 10\ny = 2"


def test_structured_edit_requires_an_exact_match() -> None:
    edit = StructuredEdit(file_path="a.py", find="return sum(values) / len(values)", replace="return 0")
    assert apply_structured_edits("def f():\n    return sum(values) / len(values)\n", [edit]).endswith("return 0\n")
    with pytest.raises(DiffError):
        apply_structured_edits("def f():\n    return sum(values)/len(values)\n", [edit])


def test_structured_edit_occurrence_is_checked() -> None:
    edit = StructuredEdit(file_path="a.py", find="x", replace="y", occurrence=3)
    with pytest.raises(DiffError):
        apply_structured_edits("x\nx\n", [edit])


@pytest.mark.parametrize(
    "path,protected",
    [
        ("tests/test_x.py", True),
        ("test_top.py", True),
        ("pkg/util_test.py", True),
        ("conftest.py", True),
        ("pyproject.toml", True),
        ("pkg/helpers.py", False),
        ("pkg/testing_utils.py", False),
    ],
)
def test_protected_test_paths(path: str, protected: bool) -> None:
    assert is_protected_test_path(path) is protected


def test_apply_hunks_can_be_used_per_file() -> None:
    parsed = parse_unified_diff(FIX_DIFF)
    assert len(parsed) == 1
    assert apply_hunks(ORIGINAL, parsed[0].hunks).startswith("def add_item(item, items=None):")
