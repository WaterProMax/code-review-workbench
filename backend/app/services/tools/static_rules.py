"""Built-in AST static rules used by the ``static_rule`` check method.

These are deliberately small and deterministic: they prove *static* facts (a
shared mutable default, a bare ``except``). They never claim to prove behaviour.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from app.schemas.enums import FindingSeverity


@dataclass
class StaticFinding:
    rule: str
    message: str
    file_path: str
    line: int
    symbol: str | None
    severity: FindingSeverity


def check_source(path: str, text: str) -> list[StaticFinding]:
    tree = ast.parse(text, filename=path)
    findings: list[StaticFinding] = []
    findings.extend(_mutable_defaults(path, tree))
    findings.extend(_bare_except(path, tree))
    findings.extend(_compare_none(path, tree))
    return findings


def _mutable_defaults(path: str, tree: ast.AST) -> list[StaticFinding]:
    out: list[StaticFinding] = []
    mutable_types = (ast.List, ast.Dict, ast.Set)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            is_mutable = isinstance(default, mutable_types) or (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id in {"list", "dict", "set"}
            )
            if is_mutable:
                out.append(
                    StaticFinding(
                        rule="B006-mutable-default",
                        message=f"{node.name} 使用共享的可变默认参数",
                        file_path=path,
                        line=getattr(default, "lineno", node.lineno),
                        symbol=node.name,
                        severity=FindingSeverity.WARNING,
                    )
                )
    return out


def _bare_except(path: str, tree: ast.AST) -> list[StaticFinding]:
    out: list[StaticFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            out.append(
                StaticFinding(
                    rule="E722-bare-except",
                    message="使用裸 except，会吞掉所有异常",
                    file_path=path,
                    line=node.lineno,
                    symbol=None,
                    severity=FindingSeverity.WARNING,
                )
            )
    return out


def _compare_none(path: str, tree: ast.AST) -> list[StaticFinding]:
    out: list[StaticFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and _is_none(comparator):
                    out.append(
                        StaticFinding(
                            rule="E711-compare-to-none",
                            message="与 None 比较应使用 is / is not",
                            file_path=path,
                            line=node.lineno,
                            symbol=None,
                            severity=FindingSeverity.INFO,
                        )
                    )
    return out


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


RULE_VERSION = "1.0"
RULE_IDS = ("B006-mutable-default", "E722-bare-except", "E711-compare-to-none")
