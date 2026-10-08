"""Read-only filesystem tools over a frozen snapshot (§7.2)."""

from __future__ import annotations

import ast
import fnmatch
import re

from pydantic import BaseModel, Field

from app.registry.tools import ToolContext, ToolResult
from app.schemas.artifacts import normalise_relpath
from app.services.tools.common import require_path_in_scope


class ListFilesArgs(BaseModel):
    pass


class ReadFileArgs(BaseModel):
    path: str = Field(min_length=1)
    max_bytes: int = Field(default=20000, ge=1, le=200000)


class SearchCodeArgs(BaseModel):
    pattern: str = Field(min_length=1)
    regex: bool = False
    path_glob: str | None = None
    max_results: int = Field(default=50, ge=1, le=500)


class AstCheckArgs(BaseModel):
    paths: list[str] = Field(default_factory=list)


async def list_files(ctx: ToolContext, _: ListFilesArgs) -> ToolResult:
    files = ctx.workspace.list_files(ctx.root_task_id, ctx.source_version)
    manifest = ctx.workspace.resolve_manifest(ctx.root_task_id, ctx.source_version)
    sizes = {e.path: e.size for e in manifest.files}
    return ToolResult.success(
        f"快照 {ctx.source_version} 有 {len(files)} 个文件",
        {"files": [{"path": p, "size": sizes.get(p, 0)} for p in files]},
    )


async def read_file(ctx: ToolContext, args: ReadFileArgs) -> ToolResult:
    path = normalise_relpath(args.path)
    require_path_in_scope(ctx, path)
    data = ctx.workspace.read_file(ctx.root_task_id, ctx.source_version, path)
    truncated = len(data) > args.max_bytes
    text = data[: args.max_bytes].decode("utf-8", errors="replace")
    return ToolResult.success(
        f"读取 {path}（{len(data)} bytes{'，已截断' if truncated else ''}）",
        {"path": path, "content": text, "size": len(data), "truncated": truncated},
    )


async def search_code(ctx: ToolContext, args: SearchCodeArgs) -> ToolResult:
    files = ctx.workspace.list_files(ctx.root_task_id, ctx.source_version)
    if args.path_glob:
        files = [f for f in files if fnmatch.fnmatch(f, args.path_glob)]
    matcher = re.compile(args.pattern) if args.regex else None
    matches: list[dict] = []
    for path in files:
        try:
            text = ctx.workspace.read_text(ctx.root_task_id, ctx.source_version, path)
        except (UnicodeDecodeError, KeyError):
            continue
        for lineno, line in enumerate(text.split("\n"), start=1):
            hit = matcher.search(line) if matcher else (args.pattern in line)
            if hit:
                matches.append({"path": path, "line": lineno, "text": line.strip()[:200]})
                if len(matches) >= args.max_results:
                    return ToolResult.success(
                        f"命中 {len(matches)} 处（已达上限）", {"matches": matches}
                    )
    return ToolResult.success(f"命中 {len(matches)} 处", {"matches": matches})


async def ast_check(ctx: ToolContext, args: AstCheckArgs) -> ToolResult:
    paths = [normalise_relpath(p) for p in args.paths] if args.paths else ctx.workspace.list_files(
        ctx.root_task_id, ctx.source_version
    )
    results: list[dict] = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        require_path_in_scope(ctx, path)
        try:
            text = ctx.workspace.read_text(ctx.root_task_id, ctx.source_version, path)
        except (UnicodeDecodeError, KeyError) as exc:
            results.append({"path": path, "ok": False, "error": str(exc)})
            continue
        try:
            tree = ast.parse(text, filename=path)
            results.append(
                {
                    "path": path,
                    "ok": True,
                    "functions": [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)],
                    "imports": [
                        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
                    ],
                }
            )
        except SyntaxError as exc:
            results.append(
                {
                    "path": path,
                    "ok": False,
                    "error": f"{exc.msg} (line {exc.lineno})",
                    "line": exc.lineno,
                }
            )
    failures = [r for r in results if not r["ok"]]
    return ToolResult.success(
        f"语法检查 {len(results)} 个文件，{len(failures)} 个失败",
        {"files": results, "ok": not failures, "failures": failures},
    )
