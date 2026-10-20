"""Docstring-only patches are rejected; a docstring edit that also changes code is kept."""

from __future__ import annotations

from app.agents.fixer import FixerFinal, spec_rewrite_rejection

DOCSTRING_ONLY = '''--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    """缺陷：减法被当成加法。"""
+    """返回两数之和。"""
     return a - b
'''

CODE_AND_DOCSTRING = '''--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    """缺陷：减法被当成加法。"""
-    return a - b
+    """返回两数之和。"""
+    return a + b
'''


def test_docstring_only_patch_is_rejected() -> None:
    final = FixerFinal(summary="改了说明", rationale="用文档掩盖缺陷", diff_text=DOCSTRING_ONLY)
    reason = spec_rewrite_rejection(final)
    assert reason is not None
    assert "文档字符串" in reason


def test_docstring_edit_that_also_changes_code_is_kept() -> None:
    final = FixerFinal(summary="修了实现", rationale="改正返回值并更新说明", diff_text=CODE_AND_DOCSTRING)
    assert spec_rewrite_rejection(final) is None
