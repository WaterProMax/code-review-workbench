"""P08 acceptance: the full flow over real HTTP (§12).

Upload → submit (202 with an id from the parent's creation record) → background
run → query detail/attempts/events/report → download an artifact → resume a
waiting task → terminate. Request retries must not create a second task, and a
fresh app instance over the same data directory must still see the task.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.providers.scripted import ScriptedLLMClient
from app.schemas.enums import RootStatus
from app.settings import ConfigurationError, Settings
from tests.fixtures.workflow import (
    BOGUS_DIFF,
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    make_scripted_client,
    sequential_workflow,
)
from tests.integration.test_extension_agent import EXTENSION_PLAN, extension_decide


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _save_workflow(client: httpx.AsyncClient, max_repair_rounds: int = 2) -> str:
    config = sequential_workflow(max_repair_rounds=max_repair_rounds)
    body = config.model_dump(mode="json")
    response = await client.post("/api/workflows", json=body)
    assert response.status_code == 201, response.text
    return response.json()["workflow_version"]


async def _upload(client: httpx.AsyncClient, files: dict[str, str]) -> str:
    payload = [
        ("files", (name, content.encode("utf-8"), "text/x-python"))
        for name, content in files.items()
    ]
    response = await client.post("/api/sources", files=payload)
    assert response.status_code == 201, response.text
    return response.json()["source_id"]


@pytest.mark.asyncio
async def test_full_flow_over_http(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api"})
    app = create_app(
        settings,
        llm_factory=lambda: make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]),
    )
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client, BUGGY_SOURCE)

        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version},
            headers={"Idempotency-Key": "submit-1"},
        )
        assert submit.status_code == 202, submit.text
        root_task_id = submit.json()["root_task_id"]
        assert root_task_id
        assert submit.json()["status"] in {"queued", "running", "completed"}

        await services.runner.wait(root_task_id, timeout=60)

        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == RootStatus.COMPLETED.value, detail
        assert detail["passed"] is True
        assert detail["contract_version"] == "contract-1"
        assert detail["detection"]["category"] == "pass"
        kids = {t["task_kind"]: t for t in detail["tasks"]}
        assert kids["review"]["status"] == "completed"
        assert kids["fix"]["status"] == "completed"
        assert kids["verify"]["passed"] is True
        checks = {c["check_id"]: c for c in detail["checks"]}
        assert checks["CHK-MUTABLE"]["status"] == "passed"
        assert checks["CHK-MUTABLE"]["required"] is True
        assert {b["budget_kind"] for b in detail["budgets"]["items"]} >= {
            "repair_round",
            "verification_retry",
            "review_retry",
        }
        assert detail["source_version"]

        # the authoritative findings endpoint reflects the *closed* state, not the
        # immutable review snapshot (whose copy still says "open")
        findings = (await client.get(f"/api/tasks/{root_task_id}/findings")).json()
        assert len(findings) == 1, findings
        assert findings[0]["required_for_goal"] is True
        assert findings[0]["status"] == "resolved"
        assert findings[0]["resolution_evidence_refs"]
        # the review artifact really does still carry the original open claim
        bundles = (await client.get(f"/api/tasks/{root_task_id}/artifacts?artifact_type=finding")).json()
        snapshot = (await client.get(f"/api/artifacts/{bundles[0]['artifact_id']}")).json()
        assert snapshot["findings"][0]["status"] == "open"
        assert snapshot["findings"][0]["resolution_evidence_refs"] == []

        attempts = (await client.get(f"/api/tasks/{root_task_id}/attempts")).json()
        assert {a["task_kind"] for a in attempts} == {"review", "fix", "verify"}
        assert all(a["result"] is not None for a in attempts)

        events = (await client.get(f"/api/tasks/{root_task_id}/events?after_seq=0")).json()
        types = {e["event_type"] for e in events["events"]}
        assert {"task_created", "task_dispatched", "result_received", "task_completed"} <= types
        assert events["next_seq"] == max(e["sequence"] for e in events["events"])
        # incremental query with no new events is well-formed
        tail = (
            await client.get(
                f"/api/tasks/{root_task_id}/events?after_seq={events['next_seq']}"
            )
        ).json()
        assert tail["events"] == []
        assert tail["next_seq"] == events["next_seq"]
        assert tail["status"] == RootStatus.COMPLETED.value

        report = (await client.get(f"/api/tasks/{root_task_id}/report")).json()
        assert report["final_status"] == RootStatus.COMPLETED.value
        assert report["passed"] is True
        assert report["report"]["checks"]

        artifact_id = report["report_artifact_id"]
        art = await client.get(f"/api/artifacts/{artifact_id}")
        assert art.status_code == 200
        assert art.json()["root_task_id"] == root_task_id

        agents = (await client.get("/api/agents")).json()
        assert any(a["agent_id"] == "reviewer" and a["version"] == "1.0" for a in agents)
        assert all("api_key" not in a for a in agents)

        listed = (await client.get("/api/tasks")).json()
        assert any(item["root_task_id"] == root_task_id for item in listed)


@pytest.mark.asyncio
async def test_submit_retry_does_not_create_a_second_task(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api2"})
    app = create_app(settings, llm_factory=lambda: make_scripted_client())
    services = app.state.services
    body = None
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client, CLEAN_SOURCE)
        body = {"source_id": source_id, "goal": "只做审查", "workflow_version": version}
        first = await client.post("/api/tasks", json=body, headers={"Idempotency-Key": "k1"})
        assert first.status_code == 202
        root_task_id = first.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)

        again = await client.post("/api/tasks", json=body, headers={"Idempotency-Key": "k1"})
        assert again.status_code == 202
        assert again.json()["root_task_id"] == root_task_id

        conflict = await client.post(
            "/api/tasks",
            json={**body, "goal": "换一个目标"},
            headers={"Idempotency-Key": "k1"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

        missing_key = await client.post("/api/tasks", json=body)
        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "VALIDATION_ERROR"

    assert services.repos.tasks.count_roots() == 1


@pytest.mark.asyncio
async def test_http_resume_after_exhausted_repair_budget(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api3"})
    # first repair round changes the file but does not fix the defect; the second does
    # (one client instance is reused, so the second round really gets the second diff)
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF, FIX_DIFF])
    app = create_app(settings, llm_factory=lambda: llm)
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client, max_repair_rounds=1)
        source_id = await _upload(client, BUGGY_SOURCE)
        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version},
            headers={"Idempotency-Key": "submit-r"},
        )
        root_task_id = submit.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)

        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == RootStatus.WAITING_RECOVERY.value, detail
        assert detail["passed"] is False
        assert "repair_round" in (detail["required_action"] or "")

        revision = detail["revision"]
        resume = await client.post(
            f"/api/tasks/{root_task_id}/resume",
            json={
                "expected_revision": revision,
                "additional_repair_rounds": 1,
                "reason": "追加一轮修复许可后恢复",
            },
            headers={"Idempotency-Key": "resume-1"},
        )
        assert resume.status_code == 200, resume.text
        assert resume.json()["status"] == RootStatus.RUNNING.value
        assert {g["budget_kind"] for g in resume.json()["granted"]} == {"repair_round"}

        # a duplicate resume returns the same outcome without granting twice
        duplicate = await client.post(
            f"/api/tasks/{root_task_id}/resume",
            json={
                "expected_revision": revision,
                "additional_repair_rounds": 1,
                "reason": "追加一轮修复许可后恢复",
            },
            headers={"Idempotency-Key": "resume-1"},
        )
        assert duplicate.status_code == 200
        assert "重复请求" in duplicate.json()["message"]

        await services.runner.wait(root_task_id, timeout=60)
        final = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert final["status"] == RootStatus.COMPLETED.value, final
        assert final["passed"] is True
        repair = next(b for b in final["budgets"]["items"] if b["budget_kind"] == "repair_round")
        assert repair["consumed"] == 2 and repair["granted_max"] == 2

        events = (await client.get(f"/api/tasks/{root_task_id}/events")).json()
        assert any(e["event_type"] == "task_resumed" for e in events["events"])


@pytest.mark.asyncio
async def test_workflow_templates_and_illegal_config_rejection(
    settings: Settings, tmp_path: Path
) -> None:
    """P10: the canvas loads real templates and cannot save an unrunnable config."""
    settings = settings.model_copy(update={"data_dir": tmp_path / "api8"})
    app = create_app(settings, llm_factory=lambda: make_scripted_client())
    async with _client(app) as client:
        templates = (await client.get("/api/workflows/templates")).json()
        by_name = {t["name"]: t for t in templates}
        assert {"workflow.sequential.yaml", "workflow.parallel.yaml"} <= set(by_name)
        sequential = by_name["workflow.sequential.yaml"]
        assert sequential["check_mode"] == "sequential"
        # expansion really happened: the input dependencies are persisted
        deps = {
            (d["source_node"], d["target_node"], d["artifact"])
            for d in sequential["config"]["dependencies"]
        }
        assert ("apply", "verify", "patch_application") in deps
        extension = by_name.get("workflow.extension.yaml")
        assert extension is not None
        assert {
            (d["source_node"], d["target_node"], d["artifact"])
            for d in extension["config"]["dependencies"]
        } >= {("review", "gen", "finding"), ("gen", "verify", "test_artifact")}

        # a legal template saves and comes back with a version + semantic hash
        saved = await client.post("/api/workflows", json=sequential["config"])
        assert saved.status_code == 201, saved.text
        version = saved.json()["workflow_version"]
        assert version and saved.json()["semantic_hash"].startswith("wf-")
        fetched = await client.get(f"/api/workflows/{version}")
        assert fetched.status_code == 200
        assert fetched.json()["semantic_hash"] == saved.json()["semantic_hash"]

        # an unregistered task kind is refused, and the error names the node
        broken = json.loads(json.dumps(sequential["config"]))
        broken["nodes"].append(
            {"id": "ghost", "type": "agent", "agent_id": "reviewer", "task_kind": "ghost_kind"}
        )
        broken["edges"].append({"source": "parent", "target": "ghost"})
        broken["edges"].append({"source": "ghost", "target": "parent"})
        broken["workflow_version"] = "wf-ghost"
        rejected = await client.post("/api/workflows", json=broken)
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["code"] == "VALIDATION_ERROR"
        assert rejected.json()["details"]["node_id"] == "ghost"

        # an unregistered role version is refused as well
        unknown_role = json.loads(json.dumps(sequential["config"]))
        unknown_role["agents"]["reviewer"] = "9.9"
        unknown_role["workflow_version"] = "wf-bad-role"
        rejected_role = await client.post("/api/workflows", json=unknown_role)
        assert rejected_role.status_code == 400
        assert rejected_role.json()["details"]["field"] == "agents.reviewer"


@pytest.mark.asyncio
async def test_extension_workflow_runs_over_http(settings: Settings, tmp_path: Path) -> None:
    """P10: the extension template is a runnable workflow, not a drawing."""
    settings = settings.model_copy(update={"data_dir": tmp_path / "api9"})
    llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING],
        plan=EXTENSION_PLAN,
        decide=extension_decide,
    )
    app = create_app(settings, llm_factory=lambda: llm)
    services = app.state.services
    async with _client(app) as client:
        templates = (await client.get("/api/workflows/templates")).json()
        extension = next(t for t in templates if t["name"] == "workflow.extension.yaml")
        saved = await client.post(
            "/api/workflows",
            json={**extension["config"], "workflow_version": "wf-ext-http"},
        )
        assert saved.status_code == 201, saved.text
        source_id = await _upload(client, BUGGY_SOURCE)
        submit = await client.post(
            "/api/tasks",
            json={
                "source_id": source_id,
                "goal": "检查可变默认参数并修复验证",
                "workflow_version": "wf-ext-http",
            },
            headers={"Idempotency-Key": "ext-http-1"},
        )
        assert submit.status_code == 202, submit.text
        root_task_id = submit.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)

        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == "completed", detail
        assert detail["passed"] is True
        # the new role is visible in the task tree like any other child
        kids = {t["task_kind"]: t for t in detail["tasks"]}
        assert kids["generate_tests"]["agent_id"] == "test_generator"
        assert kids["generate_tests"]["status"] == "completed"

        # the generated tests were executed by the verifier on the current version
        checks = {c["check_id"]: c for c in detail["checks"]}
        assert checks["CHK-BEHAVIOR"]["status"] == "passed"

        # the capability catalog exposes the extension role and its budget
        agents = (await client.get("/api/agents")).json()
        gen = next(a for a in agents if a["agent_id"] == "test_generator")
        assert gen["supported_task_kinds"] == ["generate_tests"]
        assert gen["retry_budget"] == {"generate_tests": "generate_retry"}
        assert "api_key" not in gen


@pytest.mark.asyncio
async def test_restart_keeps_tasks_queryable_and_rejects_terminate_on_running(
    settings: Settings, tmp_path: Path
) -> None:
    data_dir = tmp_path / "api4"
    settings = settings.model_copy(update={"data_dir": data_dir})
    app = create_app(settings, llm_factory=lambda: make_scripted_client())
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client, CLEAN_SOURCE)
        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "只做审查", "workflow_version": version},
            headers={"Idempotency-Key": "k-restart"},
        )
        root_task_id = submit.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)

    # a fresh app instance over the same data directory still sees the task
    app2 = create_app(settings, llm_factory=lambda: make_scripted_client())
    async with _client(app2) as client:
        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == RootStatus.COMPLETED.value
        assert detail["passed"] is True
        # a finished task cannot be terminated
        reject = await client.post(
            f"/api/tasks/{root_task_id}/terminate",
            json={"expected_revision": detail["revision"], "reason": "x"},
            headers={"Idempotency-Key": "term-1"},
        )
        assert reject.status_code == 409
        assert reject.json()["code"] == "NOT_RESUMABLE"
        # unknown task and unknown version errors are typed
        missing = await client.get("/api/tasks/T-nope")
        assert missing.status_code == 404
        assert missing.json()["code"] == "NOT_FOUND"
        unknown_wf = await client.post(
            "/api/tasks",
            json={"source_id": "s", "goal": "g", "workflow_version": "wf-missing"},
            headers={"Idempotency-Key": "k2"},
        )
        assert unknown_wf.status_code == 404
        assert unknown_wf.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_submit_without_model_configuration_returns_503(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api5"})

    def _no_model() -> ScriptedLLMClient:
        raise ConfigurationError("MODEL_NOT_CONFIGURED", "模型凭据未配置")

    app = create_app(settings, llm_factory=_no_model)
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client, CLEAN_SOURCE)
        response = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "g", "workflow_version": version},
            headers={"Idempotency-Key": "no-model"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
    # nothing was created
    assert app.state.services.repos.tasks.count_roots() == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_the_run_and_releases_the_lease(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api7"})
    # a slow scripted model keeps the run in flight so shutdown has something to cancel
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], latency_seconds=0.2)
    app = create_app(settings, llm_factory=lambda: llm)
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client, BUGGY_SOURCE)
        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version},
            headers={"Idempotency-Key": "shutdown-1"},
        )
        root_task_id = submit.json()["root_task_id"]
        # let it actually start and take the lease
        for _ in range(200):
            control = services.repos.controls.get(root_task_id)
            if control is not None and control.lease_owner:
                break
            await asyncio.sleep(0.01)
        assert services.runner.is_running(root_task_id)

        await services.runner.shutdown()
        assert not services.runner.is_running(root_task_id)
        control = services.repos.controls.get(root_task_id)
        assert control.lease_owner is None
        assert control.lease_expires_at is None

        # a restart surfaces the cancelled run as interrupted so it can be resumed
        interrupted = services.governance.recovery.recover_incomplete_runs()
        assert root_task_id in interrupted
        assert services.repos.tasks.get(root_task_id).status == RootStatus.INTERRUPTED.value


@pytest.mark.asyncio
async def test_upload_rejects_unknown_suffix_and_unsafe_path(
    settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "api6"})
    app = create_app(settings, llm_factory=lambda: make_scripted_client())
    async with _client(app) as client:
        bad_type = await client.post(
            "/api/sources", files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))]
        )
        assert bad_type.status_code == 400
        assert bad_type.json()["code"] == "VALIDATION_ERROR"

        escaped = await client.post(
            "/api/sources", files=[("files", ("../../etc/passwd.py", b"x", "text/x-python"))]
        )
        assert escaped.status_code == 400


@pytest.mark.asyncio
async def test_a34_mode_conflict_and_frozen_workflow_defaults(
    settings: Settings, tmp_path: Path
) -> None:
    """A mismatched mode is refused; a later workflow never rewrites an old task."""
    settings = settings.model_copy(update={"data_dir": tmp_path / "api-a34"})
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF])
    app = create_app(settings, llm_factory=lambda: llm)
    services = app.state.services
    async with _client(app) as client:
        version_a = await _save_workflow(client, max_repair_rounds=1)
        source_id = await _upload(client, BUGGY_SOURCE)

        # a request that contradicts the workflow's check mode is rejected
        conflict = await client.post(
            "/api/tasks",
            json={
                "source_id": source_id,
                "goal": "g",
                "workflow_version": version_a,
                "check_mode": "parallel",
            },
            headers={"Idempotency-Key": "a34-conflict"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "CONFIG_MODE_CONFLICT"

        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version_a},
            headers={"Idempotency-Key": "a34-run"},
        )
        root_task_id = submit.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)
        before = (await client.get(f"/api/tasks/{root_task_id}")).json()
        repair_before = next(
            b for b in before["budgets"]["items"] if b["budget_kind"] == "repair_round"
        )
        assert repair_before["granted_max"] == 1

        # a newer workflow with different defaults does not change the old task
        config_b = sequential_workflow(max_repair_rounds=2).model_copy(
            update={"workflow_version": "wf-a34-b"}
        )
        saved_b = await client.post("/api/workflows", json=config_b.model_dump(mode="json"))
        assert saved_b.status_code == 201, saved_b.text
        version_b = saved_b.json()["workflow_version"]
        assert version_b != version_a
        after = (await client.get(f"/api/tasks/{root_task_id}")).json()
        repair_after = next(
            b for b in after["budgets"]["items"] if b["budget_kind"] == "repair_round"
        )
        assert repair_after == repair_before
        assert after["workflow_version"] == version_a


@pytest.mark.asyncio
async def test_a35_resume_cannot_change_the_task_inputs(
    settings: Settings, tmp_path: Path
) -> None:
    """A resume request may only add budget/reason — never a new source or goal."""
    settings = settings.model_copy(update={"data_dir": tmp_path / "api-a35"})
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF])
    app = create_app(settings, llm_factory=lambda: llm)
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client, max_repair_rounds=1)
        source_id = await _upload(client, BUGGY_SOURCE)
        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version},
            headers={"Idempotency-Key": "a35-run"},
        )
        root_task_id = submit.json()["root_task_id"]
        await services.runner.wait(root_task_id, timeout=60)
        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == RootStatus.WAITING_RECOVERY.value

        # extra fields that would rewrite the task's input are rejected outright
        changed = await client.post(
            f"/api/tasks/{root_task_id}/resume",
            json={
                "expected_revision": detail["revision"],
                "reason": "换一份源码继续",
                "source_id": "s-new",
                "goal": "换一个目标",
            },
            headers={"Idempotency-Key": "a35-resume"},
        )
        assert changed.status_code == 400
        assert changed.json()["code"] == "VALIDATION_ERROR"

        # the original task and its evidence remain queryable
        still = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert still["status"] == RootStatus.WAITING_RECOVERY.value
        artifacts = (await client.get(f"/api/tasks/{root_task_id}/artifacts")).json()
        assert artifacts
        assert all(a["root_task_id"] == root_task_id for a in artifacts)
