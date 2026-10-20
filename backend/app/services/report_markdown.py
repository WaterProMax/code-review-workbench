"""Render a persisted final report as Markdown for download and JSON."""

from __future__ import annotations

from typing import Any

from app.schemas.enums import ArtifactType
from app.storage.repositories import Repos
from app.services.artifacts import ArtifactService


def render_report_markdown(
    report: dict[str, Any],
    *,
    repos: Repos,
    artifacts: ArtifactService,
    root_task_id: str,
) -> str:
    """Conclusion, checklist, findings, patch diffs and verification evidence."""
    lines: list[str] = ["# 审查报告", ""]
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 状态：{report.get('final_status', '')}")
    passed = report.get("passed")
    lines.append(f"- 通过：{_passed_label(passed)}")
    goal = report.get("goal") or ""
    if goal:
        lines.append(f"- 目标：{goal}")
    scope = report.get("conclusion_scope") or ""
    if scope:
        lines.append(f"- 结论范围：{scope}")
    lines.append("")

    lines.append("## 检查清单")
    lines.append("")
    checks = report.get("checks") or []
    if not checks:
        lines.append("（无检查项）")
    for check in checks:
        status = _status_of(check.get("status"))
        required = "必需" if check.get("required") else "可选"
        note = check.get("note") or ""
        lines.append(
            f"- {check.get('check_id', '')}：{status}（{required}）"
            + (f" — {note}" if note else "")
        )
    lines.append("")

    lines.append("## 发现")
    lines.append("")
    findings = repos.findings.list_current_by_root(root_task_id)
    if not findings:
        lines.append("（无发现）")
    for finding in findings:
        location = finding.file_path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        severity = finding.severity.value if hasattr(finding.severity, "value") else finding.severity
        status = finding.status.value if hasattr(finding.status, "value") else finding.status
        lines.append(f"- {location} {severity} {status} — {finding.message}")
    lines.append("")

    lines.append("## 补丁")
    lines.append("")
    patches = artifacts.list_by_root(root_task_id, ArtifactType.PATCH)
    if not patches:
        lines.append("（无补丁）")
    for artifact in patches:
        try:
            data = artifacts.read_json(artifact.artifact_id)
        except Exception:  # noqa: BLE001 - one bad artifact must not hide the report
            lines.append(f"### {artifact.artifact_id}")
            lines.append("")
            lines.append("（补丁无法读取）")
            lines.append("")
            continue
        title = data.get("patch_id") or artifact.artifact_id
        lines.append(f"### {title}")
        lines.append("")
        diff_text = data.get("diff_text")
        if diff_text:
            lines.append("```diff")
            lines.append(str(diff_text).rstrip("\n"))
            lines.append("```")
        edits = data.get("edits") or []
        for edit in edits:
            path = edit.get("file_path", "")
            lines.append(f"- {path}")
            lines.append("")
            lines.append("```diff")
            lines.append(f"--- a/{path}")
            lines.append(f"+++ b/{path}")
            for row in str(edit.get("find", "")).splitlines():
                lines.append(f"-{row}")
            for row in str(edit.get("replace", "")).splitlines():
                lines.append(f"+{row}")
            lines.append("```")
        lines.append("")

    lines.append("## 验证证据")
    lines.append("")
    evidence = _verification_lines(repos, root_task_id)
    if not evidence:
        lines.append("（无验证证据）")
    else:
        lines.extend(evidence)
    lines.append("")
    return "\n".join(lines)


def _verification_lines(repos: Repos, root_task_id: str) -> list[str]:
    lines: list[str] = []
    try:
        results = repos.check_results.list_by_root(root_task_id)
    except Exception:  # noqa: BLE001
        results = []
    for result in results:
        status = result.status.value if hasattr(result.status, "value") else result.status
        reason = result.reason or ""
        refs = ", ".join(result.evidence_refs) if result.evidence_refs else ""
        extra = reason
        if refs:
            extra = f"{extra}（{refs}）" if extra else refs
        lines.append(f"- {result.check_id}：{status}" + (f" — {extra}" if extra else ""))
    try:
        reports = repos.verifications.list_by_root(root_task_id)
    except Exception:  # noqa: BLE001
        reports = []
    for row in reports:
        notes = row.get("notes") or ""
        passed = row.get("passed")
        lines.append(
            f"- 验证 {row.get('verification_id', '')}：{_passed_label(passed)}"
            + (f" — {notes}" if notes else "")
        )
    return lines


def _passed_label(passed: object) -> str:
    if passed is True or passed == 1:
        return "是"
    if passed is False or passed == 0:
        return "否"
    return "未知"


def _status_of(value: object) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return "" if value is None else str(value)
