"""SSE tail of execution_events, and cancel of a queued or running root."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.api.tasks import iter_task_events
from app.main import create_app
from app.schemas.actions import DispatchItem, DispatchTaskAction
from app.schemas.api import CancelRequest, ResumeRequest
from app.schemas.common import TaskDep
from app.schemas.enums import AttemptStatus, EventType, RootStatus, TerminalOrigin
from app.schemas.tasks import InputRefs
from app.services.events import EventContext, EventSink
from app.settings import Settings
from app.workflow.recovery import RecoveryCoordinator
from tests.fixtures import builders
from tests.fixtures.workflow import BUGGY_SOURCE, make_scripted_client, sequential_workflow


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _save_workflow(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/workflows", json=sequential_workflow().model_dump(mode="json"))
    assert response.status_code == 201, response.text
    return response.json()["workflow_version"]


async def _upload(client: httpx.AsyncClient) -> str:
    payload = [
        ("files", (name, content.encode("utf-8"), "text/x-python"))
        for name, content in BUGGY_SOURCE.items()
    ]
    response = await client.post("/api/sources", files=payload)
    assert response.status_code == 201, response.text
    return response.json()["source_id"]


def _data_events(body: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]


class _DisconnectAfter:
    def __init__(self, connected_checks: int) -> None:
        self.connected_checks = connected_checks
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.connected_checks


@pytest.mark.asyncio
async def test_stream_returns_events_after_seq_with_sse_headers(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "stream"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services
    root = builders.new_root(services.repos, root_task_id="T-stream", status=RootStatus.COMPLETED.value)
    sink = EventSink(services.repos.events, EventContext(root_task_id=root.task_id, actor_id="test"))
    first = sink.emit(EventType.TASK_CREATED, payload={"step": 1})
    second = sink.emit(EventType.TASK_COMPLETED, payload={"step": 2})

    async with _client(app) as client:
        response = await asyncio.wait_for(client.get(f"/api/tasks/{root.task_id}/stream"), timeout=5)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        events = _data_events(response.text)
        assert [item["event_id"] for item in events] == [first.event_id, second.event_id]
        assert [item["sequence"] for item in events] == [1, 2]
        assert ": heartbeat" not in response.text

        tail = await asyncio.wait_for(
            client.get(f"/api/tasks/{root.task_id}/stream?after_seq=1"), timeout=5
        )
        assert [item["event_id"] for item in _data_events(tail.text)] == [second.event_id]

        missing = await client.get("/api/tasks/does-not-exist/stream")
        assert missing.status_code == 404

        resumed = await asyncio.wait_for(
            client.get(
                f"/api/tasks/{root.task_id}/stream",
                params={"after_seq": 0},
                headers={"Last-Event-ID": str(first.sequence)},
            ),
            timeout=5,
        )
        assert f"id: {second.sequence}" in resumed.text
        assert f"id: {first.sequence}" not in resumed.text
        assert [item["event_id"] for item in _data_events(resumed.text)] == [second.event_id]


@pytest.mark.asyncio
async def test_stream_heartbeats_until_the_client_disconnects(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "stream-idle"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services
    root = builders.new_root(services.repos, root_task_id="T-idle", status=RootStatus.RUNNING.value)
    request = _DisconnectAfter(connected_checks=1)
    chunks: list[str] = []
    async for chunk in iter_task_events(request, services, root.task_id, 0, idle_seconds=0.01):  # type: ignore[arg-type]
        chunks.append(chunk)
    assert chunks == [": heartbeat\n\n"]
    assert request.calls == 2


@pytest.mark.asyncio
async def test_cancel_checks_revision_and_foreign_lease(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "cancel-fence"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services

    async with _client(app) as client:
        # Create the row after startup. A queued root that exists at startup is
        # scanned as interrupted because it has no lease yet.
        queued = builders.new_root(services.repos, root_task_id="T-queued", status=RootStatus.QUEUED.value)
        stale = await client.post(
            f"/api/tasks/{queued.task_id}/cancel",
            json={"expected_revision": 99, "reason": "过期修订"},
            headers={"Idempotency-Key": "cancel-stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "VERSION_CONFLICT"
        assert services.repos.tasks.get(queued.task_id).status == RootStatus.QUEUED.value

        ok = await client.post(
            f"/api/tasks/{queued.task_id}/cancel",
            json={"expected_revision": 0, "reason": "用户取消排队任务"},
            headers={"Idempotency-Key": "cancel-queued"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == RootStatus.WAITING_RECOVERY.value
        assert ok.json()["signalled"] is False
        assert services.governance.recovery.can_resume(queued.task_id) == []

        again = await client.post(
            f"/api/tasks/{queued.task_id}/cancel",
            json={"expected_revision": 0, "reason": "用户取消排队任务"},
            headers={"Idempotency-Key": "cancel-queued"},
        )
        assert again.status_code == 200
        other = await client.post(
            f"/api/tasks/{queued.task_id}/cancel",
            json={"expected_revision": ok.json()["revision"], "reason": "再次取消"},
            headers={"Idempotency-Key": "cancel-queued-2"},
        )
        assert other.status_code == 409
        assert other.json()["code"] == "NOT_RESUMABLE"

        running = builders.new_root(services.repos, root_task_id="T-leased", status=RootStatus.RUNNING.value)
        child = builders.new_child(
            services.repos, running, task_id="T-leased-C1", task_kind="review", status="running"
        )
        attempt = builders.new_attempt(
            services.repos, running, child, attempt_id="T-leased-C1-A1", batch_id="B-leased"
        )
        services.repos.attempts.update_status(
            attempt.attempt_id, AttemptStatus.RUNNING, started_at=datetime.now(timezone.utc)
        )
        services.repos.controls.ensure(running.task_id, "seg-leased", 32)
        acquired, control = services.locks.acquire(running.task_id, "other-owner", ttl_seconds=120)
        assert acquired
        token = control.fencing_token

        refused = await client.post(
            f"/api/tasks/{running.task_id}/cancel",
            json={"expected_revision": running.revision, "reason": "别人的租约"},
            headers={"Idempotency-Key": "cancel-foreign"},
        )
        assert refused.status_code == 409
        assert refused.json()["code"] == "CONFLICT"
        assert services.repos.tasks.get(running.task_id).status == RootStatus.RUNNING.value
        assert services.repos.controls.get(running.task_id).fencing_token == token
        assert services.repos.controls.get(running.task_id).lease_owner == "other-owner"
        assert services.repos.terminals.get(attempt.attempt_id) is None

        # The in-process owner may cancel, and must fence the previous token.
        services.repos.controls.update(running.task_id, release_lease=True)
        acquired_own, owned = services.locks.acquire(
            running.task_id, services.runner.owner, ttl_seconds=120
        )
        assert acquired_own
        owned_token = owned.fencing_token
        accepted = await client.post(
            f"/api/tasks/{running.task_id}/cancel",
            json={"expected_revision": running.revision, "reason": "本进程取消"},
            headers={"Idempotency-Key": "cancel-owner"},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == RootStatus.WAITING_RECOVERY.value
        assert accepted.json()["signalled"] is False
    updated = services.repos.controls.get(running.task_id)
    assert updated.lease_owner is None
    assert updated.fencing_token == owned_token + 1
    assert not services.locks.has_right(running.task_id, owned_token, owner=services.runner.owner)
    terminal = services.repos.terminals.get(attempt.attempt_id)
    assert terminal is not None
    assert terminal.origin is TerminalOrigin.CONTROLLER
    assert terminal.outcome.value == "failed"
    assert services.repos.attempts.get(attempt.attempt_id).status == AttemptStatus.INVALIDATED.value
    assert services.repos.tasks.get(child.task_id).status == "cancelled"
    assert services.repos.attempts.list_active(running.task_id) == []
    assert services.governance.recovery.can_resume(running.task_id) == []
    assert isinstance(services.governance.recovery, RecoveryCoordinator)


@pytest.mark.asyncio
async def test_cancel_running_task_signals_the_live_run(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "cancel-live"})
    app = create_app(
        settings,
        llm_factory=lambda: make_scripted_client(latency_seconds=30),
    )
    services = app.state.services
    async with _client(app) as client:
        version = await _save_workflow(client)
        source_id = await _upload(client)
        submit = await client.post(
            "/api/tasks",
            json={"source_id": source_id, "goal": "修复可变默认参数并验证", "workflow_version": version},
            headers={"Idempotency-Key": "cancel-live-submit"},
        )
        assert submit.status_code == 202, submit.text
        root_task_id = submit.json()["root_task_id"]
        for _ in range(400):
            control = services.repos.controls.get(root_task_id)
            if control is not None and control.lease_owner and services.runner.is_running(root_task_id):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("background run did not take the lease")
        signal = services.runner.cancellation_event(root_task_id)
        assert signal is not None and not signal.is_set()
        old_token = services.repos.controls.get(root_task_id).fencing_token

        stale = await client.post(
            f"/api/tasks/{root_task_id}/cancel",
            json={"expected_revision": 1_000_000, "reason": "过期修订"},
            headers={"Idempotency-Key": "cancel-live-stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "VERSION_CONFLICT"
        assert services.runner.is_running(root_task_id)
        assert not signal.is_set()
        assert services.repos.controls.get(root_task_id).fencing_token == old_token

        accepted = None
        for _ in range(8):
            current = services.repos.tasks.get(root_task_id)
            assert current is not None
            accepted = await client.post(
                f"/api/tasks/{root_task_id}/cancel",
                json={"expected_revision": current.revision, "reason": "用户取消运行中的任务"},
                headers={"Idempotency-Key": "cancel-live"},
            )
            if accepted.status_code == 200:
                break
            assert accepted.status_code == 409 and accepted.json()["code"] == "VERSION_CONFLICT"
            await asyncio.sleep(0.05)
        assert accepted is not None and accepted.status_code == 200, accepted.text if accepted else "no response"
        assert accepted.json()["signalled"] is True
        assert signal.is_set()

        await services.runner.wait(root_task_id, timeout=5)
        assert not services.runner.is_running(root_task_id)
        root = services.repos.tasks.get(root_task_id)
        assert root is not None
        assert root.status == RootStatus.WAITING_RECOVERY.value
        detail = (await client.get(f"/api/tasks/{root_task_id}")).json()
        assert detail["status"] == RootStatus.WAITING_RECOVERY.value
        control = services.repos.controls.get(root_task_id)
        assert control.lease_owner is None
        assert control.fencing_token > old_token
        assert not services.locks.has_right(root_task_id, old_token, owner=services.runner.owner)
        assert services.repos.attempts.list_active(root_task_id) == []
        assert services.governance.recovery.can_resume(root_task_id) == []

        terminated = await client.post(
            f"/api/tasks/{root_task_id}/terminate",
            json={"expected_revision": root.revision, "reason": "取消后终止"},
            headers={"Idempotency-Key": "cancel-live-terminate"},
        )
        assert terminated.status_code == 200, terminated.text
        assert terminated.json()["status"] in {RootStatus.PARTIAL.value, RootStatus.COMPLETED.value}


@pytest.mark.asyncio
async def test_cancelled_child_can_be_redispatched(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "cancel-redispatch"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services
    workflow = sequential_workflow()
    services.repos.workflows.insert(workflow)
    root = builders.new_root(
        services.repos,
        root_task_id="T-cancel-again",
        status=RootStatus.RUNNING.value,
        workflow_version=workflow.workflow_version or "wf-1",
    )
    child = builders.new_child(
        services.repos, root, task_id="T-cancel-again-C1", task_kind="review", status="running"
    )
    attempt = builders.new_attempt(
        services.repos, root, child, attempt_id="T-cancel-again-C1-A1", batch_id="B-cancel-again"
    )
    services.repos.attempts.update_status(
        attempt.attempt_id, AttemptStatus.RUNNING, started_at=datetime.now(timezone.utc)
    )
    services.repos.controls.ensure(root.task_id, "seg-cancel-again", 32)
    acquired, _control = services.locks.acquire(root.task_id, services.runner.owner, ttl_seconds=120)
    assert acquired
    services.governance.recovery.cancel(
        root.task_id,
        CancelRequest(expected_revision=root.revision, reason="取消后准备重派"),
        owner=services.runner.owner,
        operation_key="cancel-redispatch",
    )
    cancelled = services.repos.tasks.get(child.task_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert services.repos.attempts.get(attempt.attempt_id).status == AttemptStatus.INVALIDATED.value
    assert services.governance.task_service.has_in_flight_work(root.task_id) is False

    action = DispatchTaskAction(
        task=DispatchItem(
            agent_id="fixer",
            task_kind="fix",
            goal="在取消的审查之后修复",
            input_refs=InputRefs(source="art-source", acceptance_contract="art-contract"),
            acceptance_criteria=["缺陷被修复"],
            depends_on=[TaskDep(task_id=child.task_id, condition="review_completed")],
            reason="取消的子任务视为已结束",
        )
    )
    problems = services.governance.controller.validate_action(
        action,
        root_task_id=root.task_id,
        workflow=workflow,
        source_version="sv-1",
    )
    assert not any("尚未结束" in problem for problem in problems)

    current = services.repos.tasks.get(root.task_id)
    assert current is not None
    services.governance.recovery.resume(
        root.task_id,
        ResumeRequest(expected_revision=current.revision, reason="重新派发已取消的子任务"),
        budgets=workflow.budgets,
        owner=services.runner.owner,
        operation_key="resume-redispatch",
    )
    item = DispatchItem(
        agent_id="reviewer",
        task_kind="review",
        goal="重新审查",
        input_refs=InputRefs(source="art-source", acceptance_contract="art-contract"),
        acceptance_criteria=["指出缺陷"],
        task_id=child.task_id,
        reason="取消后重新派发",
    )
    reason = services.governance.controller.retry_reason(item, source_version="sv-1")
    control = services.repos.controls.get(root.task_id)
    assert control is not None
    _batch, attempts = services.governance.task_service.register_dispatch(
        root_task_id=root.task_id,
        items=[item],
        workflow=workflow,
        source_version="sv-1",
        contract_version="contract-1",
        retry_reason=reason,
        fencing_token=control.fencing_token,
    )
    assert attempts[-1].attempt_id != attempt.attempt_id
    assert services.repos.tasks.get(child.task_id).status == "queued"
    assert len(services.repos.attempts.list_by_task(child.task_id)) == 2
