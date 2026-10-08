"""Shared helpers for tool implementations."""

from __future__ import annotations

import fnmatch

from app.registry.tools import ToolContext, ToolError


def require_path_in_scope(ctx: ToolContext, path: str) -> None:
    """Enforce the dispatch's allowed file scope, when one was declared."""
    if not ctx.allowed_paths:
        return
    for allowed in ctx.allowed_paths:
        if path == allowed or fnmatch.fnmatch(path, allowed) or path.startswith(allowed.rstrip("/") + "/"):
            return
    raise ToolError("TOOL_OUT_OF_SCOPE", f"{path} 不在本次允许的文件范围内")
