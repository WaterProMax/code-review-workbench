"""``run_checks``: execute contract checks and produce CheckResults.

Supported methods (ImplementationPlan §5.3, §7.5):

* ``syntax``      — every scoped Python file must parse.
* ``static_rule`` — declared rule ids must report no hits in scope.
* ``behavior_test``— the fixed pytest executor runs the check's tests (generated
  tests for the check plus, optionally, the project's existing tests) and the
  outcome is classified; zero collected / all skipped / collection error /
  missing dependency / timeout never yield ``passed``.
* ``model_review`` — not executable by a tool; it is the agent's own analysis.
  ``run_checks`` reports it as ``not_run`` here and the reviewer records the
  check result itself with its reads and evidence positions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid

from pydantic import BaseModel, Field

from app.registry.tools import ToolContext, ToolResult
from app.schemas.artifacts import GeneratedTestSuite
from app.schemas.enums import ArtifactType, CheckMethod, CheckStatus
from app.schemas.results import CheckResult, CheckSpec
from app.services.tools import static_rules
from app.services.tools.common import require_path_in_scope
from app.services.tools.check_scope import resolve_scope, scope_matches
from app.services.tools.test_executor import (
    PytestOutcome,
    cleanup_execution_dir,
    run_pytest,
    set_up_execution_dir,
)

EXECUTOR_VERSION = "1.1"
GENERATED_TESTS_DIR = "_hw2_tests"


class RunChecksArgs(BaseModel):
    checks: list[CheckSpec] = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    generated_tests: dict[str, list[str]] = Field(default_factory=dict)
    include_existing_tests: bool = True
    baseline_version: str | None = None
    repair_check_ids: list[str] | None = None


async def run_checks(ctx: ToolContext, args: RunChecksArgs) -> ToolResult:
    all_files = ctx.workspace.list_files(ctx.root_task_id, ctx.source_version)
    results: list[CheckResult] = []
    findings: list[dict] = []
    faults: list[dict] = []
    evidence_refs: list[str] = []

    for check in args.checks:
        invalid_rules = sorted(set(check.rule_ids) - set(static_rules.RULE_IDS))
        missing_scope = [s for s in check.scope if not scope_matches(s, all_files)]
        no_python = (check.method in {CheckMethod.SYNTAX, CheckMethod.STATIC_RULE}
                     and not any(p.endswith(".py") for p in _scope_files(check, all_files)))
        if invalid_rules or missing_scope or no_python:
            result = CheckResult(
                check_id=check.check_id, contract_version=args.contract_version,
                source_version=ctx.source_version,
                producer_attempt_id=ctx.attempt_id or "unknown", method=check.method,
                status=CheckStatus.NOT_RUN, executed=False,
                reason=f"无法执行合同检查：未注册规则={invalid_rules}，未匹配范围={missing_scope}，范围内无 Python 文件={no_python}",
            )
            results.append(result)
            continue
        if check.method is CheckMethod.SYNTAX:
            result, refs = _run_syntax(ctx, args, check, all_files)
        elif check.method is CheckMethod.STATIC_RULE:
            result, refs, new_findings = _run_static(ctx, args, check, all_files)
            findings.extend(new_findings)
        elif check.method is CheckMethod.BEHAVIOR_TEST:
            result, refs, outcome = await _run_behavior(ctx, args, check, all_files)
            if outcome is not None:
                fault = _fault_for(check, outcome)
                if fault:
                    faults.append(fault)
        else:  # model_review
            result = CheckResult(
                check_id=check.check_id,
                contract_version=args.contract_version,
                source_version=ctx.source_version,
                producer_attempt_id=ctx.attempt_id or "unknown",
                method=check.method,
                status=CheckStatus.NOT_RUN,
                executed=False,
                reason="model_review 由审查角色自行分析并记录结果，不由工具执行",
                evidence_refs=[],
            )
            refs = []
        results.append(result)
        evidence_refs.extend(refs)

    for result in results:
        ctx.repos.check_results.insert(result, ctx.root_task_id, refresh=True)

    bundle = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.CHECK_RESULT,
        data={
            "source_version": ctx.source_version,
            "contract_version": args.contract_version,
            "executor_version": EXECUTOR_VERSION,
            "results": [r.model_dump(mode="json") for r in results],
            "findings": findings,
        },
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"checks-{uuid.uuid4().hex[:8]}",
    )
    evidence_refs.append(bundle.artifact_id)

    passed = sum(1 for r in results if r.status is CheckStatus.PASSED)
    return ToolResult(
        ok=True,
        summary=f"执行 {len(results)} 项检查：{passed} passed，{len(faults)} 项执行故障",
        data={
            "check_results": [r.model_dump(mode="json") for r in results],
            "findings": findings,
            "faults": faults,
            "source_version": ctx.source_version,
            "executor_version": EXECUTOR_VERSION,
        },
        artifact_refs=[bundle.artifact_id],
        faults=faults,
    )


# --------------------------------------------------------------------------- #
# syntax
# --------------------------------------------------------------------------- #
def _run_syntax(
    ctx: ToolContext, args: RunChecksArgs, check: CheckSpec, all_files: list[str]
) -> tuple[CheckResult, list[str]]:
    import ast

    files = _scope_files(check, all_files)
    failures: list[dict] = []
    checked: list[str] = []
    for path in files:
        if not path.endswith(".py"):
            continue
        require_path_in_scope(ctx, path)
        checked.append(path)
        try:
            ast.parse(ctx.workspace.read_text(ctx.root_task_id, ctx.source_version, path), filename=path)
        except SyntaxError as exc:
            failures.append({"path": path, "line": exc.lineno, "message": exc.msg})
    evidence = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.EVIDENCE,
        data={"check_id": check.check_id, "method": "syntax", "checked": checked, "failures": failures},
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"syntax-{check.check_id}",
    )
    status = CheckStatus.FAILED if failures else CheckStatus.PASSED
    reason = (
        f"{len(failures)} 个文件语法错误：{failures[0]['path']}:{failures[0].get('line')}"
        if failures
        else f"{len(checked)} 个文件可解析（仅证明可解析，不证明行为正确）"
    )
    return (
        CheckResult(
            check_id=check.check_id,
            contract_version=args.contract_version,
            source_version=ctx.source_version,
            producer_attempt_id=ctx.attempt_id or "unknown",
            method=CheckMethod.SYNTAX,
            status=status,
            evidence_refs=[evidence.artifact_id],
            reason=reason,
        ),
        [evidence.artifact_id],
    )


# --------------------------------------------------------------------------- #
# static rules
# --------------------------------------------------------------------------- #
def _run_static(
    ctx: ToolContext, args: RunChecksArgs, check: CheckSpec, all_files: list[str]
) -> tuple[CheckResult, list[str], list[dict]]:
    files = _scope_files(check, all_files)
    hits: list[dict] = []
    for path in files:
        if not path.endswith(".py"):
            continue
        require_path_in_scope(ctx, path)
        text = ctx.workspace.read_text(ctx.root_task_id, ctx.source_version, path)
        for finding in static_rules.check_source(path, text):
            if finding.rule in check.rule_ids:
                hits.append(
                    {
                        "rule": finding.rule,
                        "file_path": finding.file_path,
                        "line": finding.line,
                        "symbol": finding.symbol,
                        "message": finding.message,
                        "severity": finding.severity.value,
                        "check_id": check.check_id,
                        "goal_ref": check.goal_ref,
                    }
                )
    evidence = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.EVIDENCE,
        data={
            "check_id": check.check_id,
            "method": "static_rule",
            "rule_ids": check.rule_ids,
            "rule_version": static_rules.RULE_VERSION,
            "files": files,
            "hits": hits,
        },
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"static-{check.check_id}",
    )
    status = CheckStatus.FAILED if hits else CheckStatus.PASSED
    reason = (
        f"命中 {len(hits)} 条规则问题（如 {hits[0]['file_path']}:{hits[0]['line']} {hits[0]['rule']}）"
        if hits
        else f"{check.rule_ids} 在 {len(files)} 个文件范围内无命中"
    )
    return (
        CheckResult(
            check_id=check.check_id,
            contract_version=args.contract_version,
            source_version=ctx.source_version,
            producer_attempt_id=ctx.attempt_id or "unknown",
            method=CheckMethod.STATIC_RULE,
            status=status,
            evidence_refs=[evidence.artifact_id],
            reason=reason,
        ),
        [evidence.artifact_id],
        hits,
    )


# --------------------------------------------------------------------------- #
# behaviour tests
# --------------------------------------------------------------------------- #
def _expand_generated(content: str, check_id: str) -> list[tuple[str, str]]:
    """One artifact may be a single test file or a generated-test bundle.

    A bundle (P10 extension) groups tests for several checks; only the entries for
    this check are expanded. Anything else is treated as one file, so the base
    verifier path is unchanged.
    """
    try:
        suite = GeneratedTestSuite.model_validate(json.loads(content))
    except Exception:  # noqa: BLE001 - not a bundle: a plain test file
        return [("", content)]
    selected = suite.by_check().get(check_id, [])
    if not selected:
        return []
    return [(f"-{index}", test.content) for index, test in enumerate(selected)]


async def _run_behavior(
    ctx: ToolContext, args: RunChecksArgs, check: CheckSpec, all_files: list[str]
) -> tuple[CheckResult, list[str], PytestOutcome | None]:
    generated: dict[str, str] = {}
    for artifact_id in args.generated_tests.get(check.check_id, []):
        ctx.artifacts.verify_ownership(artifact_id, ctx.root_task_id)
        content = ctx.artifacts.read_bytes(artifact_id).decode("utf-8")
        for suffix, source in _expand_generated(content, check.check_id):
            generated[f"{GENERATED_TESTS_DIR}/{check.check_id}/{artifact_id}{suffix}.py"] = source

    existing: dict[str, str] = {}
    if args.include_existing_tests:
        for path in all_files:
            if _is_test_file(path):
                existing[path] = ctx.workspace.read_text(ctx.root_task_id, ctx.source_version, path)

    targets = {**generated, **existing}
    if not targets:
        return (
            CheckResult(
                check_id=check.check_id,
                contract_version=args.contract_version,
                source_version=ctx.source_version,
                producer_attempt_id=ctx.attempt_id or "unknown",
                method=CheckMethod.BEHAVIOR_TEST,
                status=CheckStatus.NOT_RUN,
                executed=False,
                reason="没有可执行的测试输入（既无生成测试，也没有已有测试文件）",
            ),
            [],
            None,
        )

    base_dir = ctx.workspace.settings.evidence_dir / ctx.root_task_id / "runs"
    base_dir.mkdir(parents=True, exist_ok=True)
    run_dir, written = set_up_execution_dir(
        base_dir=base_dir,
        snapshot_files=ctx.workspace.snapshot_files(ctx.root_task_id, ctx.source_version),
        test_files=targets,
    )
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        run_pytest, run_dir, sorted(written.keys()), ctx.tool_timeout_seconds, cancelled
    ))
    try:
        outcome = await asyncio.shield(worker)
    finally:
        cancelled.set()
        await worker
        cleanup_execution_dir(run_dir)

    suite_hash = _suite_hash(written)
    generated_hash = hashlib.sha256(json.dumps(sorted(generated.values()), ensure_ascii=False).encode()).hexdigest()
    evidence = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.EVIDENCE,
        data={
            "check_id": check.check_id,
            "method": "behavior_test",
            "executor_version": EXECUTOR_VERSION,
            "source_version": ctx.source_version,
            "test_suite_hash": suite_hash,
            "generated_suite_hash": generated_hash,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "counts": outcome.counts,
            "summary": outcome.summary_line,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
            "targets": outcome.target,
        },
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"behavior-{check.check_id}",
    )

    # baseline proof of the defect (A27): same generated tests on the pre-fix version
    baseline_note = ""
    base_outcome = None
    if args.baseline_version and generated:
        base_outcome = await _run_baseline(ctx, check, generated, args.baseline_version)
        if base_outcome is not None:
            baseline_note = (
                f"；基础版本 {args.baseline_version} 上同一测试：{base_outcome.summary_line}"
            )

    status, reason = _classify(outcome)
    if status is CheckStatus.PASSED and args.baseline_version and generated:
        prior = ctx.repos.check_results.latest_for_check(
            ctx.root_task_id, check.check_id, args.baseline_version
        )
        # A previously passing contract check is regression coverage. A repaired
        # defect (or an unproven baseline) still needs a failing reproduction.
        regression = (args.repair_check_ids is not None
                      and check.check_id not in args.repair_check_ids
                      and base_outcome is not None
                      and _classify(base_outcome)[0] is CheckStatus.PASSED)
        if (prior is not None and prior.status is CheckStatus.PASSED and base_outcome is not None
                and (args.repair_check_ids is None or check.check_id not in args.repair_check_ids)):
            for ref in prior.evidence_refs:
                proof = ctx.artifacts.read_json(ref)
                if proof.get("generated_suite_hash") == generated_hash:
                    regression = _classify(base_outcome)[0] is CheckStatus.PASSED
                    break
        if not regression and (base_outcome is None or _classify(base_outcome)[0] is not CheckStatus.FAILED):
            status = CheckStatus.INCONCLUSIVE
            reason = "生成测试未在基础版本证明目标缺陷，不能作为修复通过证据"
    return (
        CheckResult(
            check_id=check.check_id,
            contract_version=args.contract_version,
            source_version=ctx.source_version,
            producer_attempt_id=ctx.attempt_id or "unknown",
            method=CheckMethod.BEHAVIOR_TEST,
            status=status,
            evidence_refs=[evidence.artifact_id],
            reason=reason + baseline_note,
            test_count=outcome.collected,
            passed_count=outcome.passed,
            failed_count=outcome.failed,
            skipped_count=outcome.skipped,
            test_suite_hash=suite_hash,
            executed=status is not CheckStatus.NOT_RUN,
        ),
        [evidence.artifact_id],
        outcome,
    )


async def _run_baseline(
    ctx: ToolContext, check: CheckSpec, generated: dict[str, str], baseline_version: str
) -> PytestOutcome | None:
    try:
        snapshot = ctx.workspace.snapshot_files(ctx.root_task_id, baseline_version)
    except KeyError:
        return None
    base_dir = ctx.workspace.settings.evidence_dir / ctx.root_task_id / "baseline"
    base_dir.mkdir(parents=True, exist_ok=True)
    run_dir, written = set_up_execution_dir(
        base_dir=base_dir, snapshot_files=snapshot, test_files=generated
    )
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        run_pytest, run_dir, sorted(written.keys()), ctx.tool_timeout_seconds, cancelled
    ))
    try:
        outcome = await asyncio.shield(worker)
    finally:
        cancelled.set()
        await worker
        cleanup_execution_dir(run_dir)
    ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.EVIDENCE,
        data={
            "check_id": check.check_id,
            "kind": "baseline",
            "source_version": baseline_version,
            "test_suite_hash": _suite_hash(written),
            "exit_code": outcome.exit_code,
            "counts": outcome.counts,
            "summary": outcome.summary_line,
            "stdout": outcome.stdout,
        },
        producer_attempt_id=ctx.attempt_id,
        source_version=baseline_version,
        name=f"baseline-{check.check_id}",
    )
    return outcome


def _classify(outcome: PytestOutcome) -> tuple[CheckStatus, str]:
    if outcome.timed_out:
        return CheckStatus.NOT_RUN, f"测试执行超时：{outcome.summary_line}"
    if outcome.missing_dependency:
        return CheckStatus.NOT_RUN, f"缺少依赖 {outcome.missing_dependency}，无法运行测试"
    if outcome.collection_error:
        return CheckStatus.NOT_RUN, f"测试收集失败：{outcome.summary_line}"
    if outcome.collected == 0:
        return CheckStatus.NOT_RUN, "零收集测试，不能作为通过证据"
    if outcome.skipped == outcome.collected:
        return CheckStatus.INCONCLUSIVE, "全部测试被跳过，不能作为通过证据"
    if outcome.failed > 0:
        return CheckStatus.FAILED, f"存在失败测试：{outcome.summary_line}"
    if outcome.passed >= 1 and outcome.exit_code == 0:
        return CheckStatus.PASSED, f"{outcome.passed} 项测试通过（{outcome.summary_line}）"
    return CheckStatus.NOT_RUN, f"执行器返回异常状态（exit={outcome.exit_code}）：{outcome.summary_line}"


def _fault_for(check: CheckSpec, outcome: PytestOutcome) -> dict | None:
    if outcome.timed_out:
        return {
            "check_id": check.check_id,
            "code": "TOOL_TIMEOUT",
            "category": "tool_timeout",
            "message": "测试执行超时，已终止进程组",
            "recoverable": True,
        }
    if outcome.missing_dependency:
        return {
            "check_id": check.check_id,
            "code": "MISSING_DEPENDENCY",
            "category": "missing_dependency",
            "message": f"缺少依赖 {outcome.missing_dependency}",
            "recoverable": True,
        }
    if outcome.collection_error:
        return {
            "check_id": check.check_id,
            "code": "TEST_COLLECTION_ERROR",
            "category": "tool_error",
            "message": outcome.summary_line or "测试收集失败",
            "recoverable": True,
        }
    if outcome.collected == 0 and outcome.exit_code not in (0, 1, 5):
        return {
            "check_id": check.check_id,
            "code": "TEST_RUNNER_ERROR",
            "category": "tool_error",
            "message": f"测试进程异常退出（exit={outcome.exit_code}）",
            "recoverable": True,
        }
    return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _scope_files(check: CheckSpec, all_files: list[str]) -> list[str]:
    return resolve_scope(check.scope, all_files)


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _suite_hash(files: dict[str, str]) -> str:
    payload = json.dumps(sorted(files.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
