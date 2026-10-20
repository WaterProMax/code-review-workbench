"""Downloads, the runtime graph, bearer auth, and SSE resume."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.providers.null import NullLLM
from app.schemas.enums import ArtifactType, FindingSeverity
from app.schemas.results import Finding
from app.settings import Settings
from app.workflow.assembly import assemble_workflow, compile_graph
from tests.fixtures import builders
from tests.fixtures.workflow import make_scripted_client, sequential_workflow

_HIDDEN = {"__start__", "__end__"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_source_zip_and_report_markdown(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "downloads"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services
    workflow = sequential_workflow()
    services.repos.workflows.insert(workflow)
    root = builders.new_root(
        services.repos,
        root_task_id="T-download",
        status="completed",
        workflow_version=workflow.workflow_version or "wf-1",
    )
    other = builders.new_root(services.repos, root_task_id="T-other", status="completed")
    payload = {"calc.py": b"def add(a, b):\n    return a + b\n"}
    manifest = services.workspace.publish_snapshot(root_task_id=root.task_id, files=payload)
    services.workspace.register_source_artifact(
        root_task_id=root.task_id, source_version=manifest.source_version
    )
    other_manifest = services.workspace.publish_snapshot(
        root_task_id=other.task_id, files={"other.py": b"x = 1\n"}
    )
    report = {
        "final_status": "completed",
        "passed": True,
        "goal": "修复加法",
        "conclusion_scope": "结论只覆盖 calc.py",
        "checks": [
            {"check_id": "syntax", "status": "passed", "required": True, "note": "语法通过"}
        ],
    }
    artifact = services.artifacts.save_json(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.FINAL_REPORT,
        data=report,
        source_version=manifest.source_version,
        name="final-report",
    )
    services.repos.reports.insert(
        report_id="report-1",
        root_task_id=root.task_id,
        kind="final",
        artifact_id=artifact.artifact_id,
        final_status="completed",
        passed=True,
    )
    services.repos.findings.upsert(
        Finding(
            finding_id="F1",
            root_task_id=root.task_id,
            source_version=manifest.source_version,
            producer_attempt_id="attempt-1",
            file_path="calc.py",
            line=2,
            rule="wrong-operator",
            severity=FindingSeverity.ERROR,
            message="加法写成了减法",
        )
    )
    services.artifacts.save_json(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.PATCH,
        data={"patch_id": "P1", "diff_text": "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-old\n+new\n"},
        source_version=manifest.source_version,
        name="patch-P1",
    )

    async with _client(app) as client:
        missing = await client.get("/api/tasks/does-not-exist/source.zip")
        assert missing.status_code == 404

        foreign = await client.get(
            f"/api/tasks/{root.task_id}/source.zip",
            params={"version": other_manifest.source_version},
        )
        assert foreign.status_code == 404

        unknown_version = await client.get(
            f"/api/tasks/{root.task_id}/source.zip", params={"version": "sv-not-this-task"}
        )
        assert unknown_version.status_code == 404

        downloaded = await client.get(f"/api/tasks/{root.task_id}/source.zip")
        assert downloaded.status_code == 200, downloaded.text
        filename = f"{root.task_id}-{manifest.source_version[:8]}-fixed.zip"
        assert filename in downloaded.headers["content-disposition"]
        archive = zipfile.ZipFile(io.BytesIO(downloaded.content))
        assert archive.namelist() == ["calc.py"]
        assert hashlib.sha256(archive.read("calc.py")).hexdigest() == hashlib.sha256(payload["calc.py"]).hexdigest()

        explicit = await client.get(
            f"/api/tasks/{root.task_id}/source.zip",
            params={"version": manifest.source_version},
        )
        assert explicit.status_code == 200
        assert hashlib.sha256(zipfile.ZipFile(io.BytesIO(explicit.content)).read("calc.py")).hexdigest() == (
            hashlib.sha256(payload["calc.py"]).hexdigest()
        )

        no_report = await client.get(f"/api/tasks/{other.task_id}/report.md")
        assert no_report.status_code == 404
        assert (await client.get(f"/api/tasks/{other.task_id}/report")).status_code == 404

        markdown = await client.get(f"/api/tasks/{root.task_id}/report.md")
        assert markdown.status_code == 200, markdown.text
        body = markdown.text
        assert "结论只覆盖 calc.py" in body
        assert "syntax" in body
        assert "calc.py:2" in body
        assert "error" in body
        assert "--- a/calc.py" in body

        json_report = await client.get(f"/api/tasks/{root.task_id}/report")
        assert json_report.status_code == 200
        assert json_report.json()["markdown"] == body

        detail = await client.get(f"/api/tasks/{root.task_id}")
        assert detail.status_code == 200
        assert detail.json()["current_nodes"] == []


@pytest.mark.asyncio
async def test_workflow_graph_matches_compiled_nodes(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "graph"})
    app = create_app(settings, llm_factory=make_scripted_client)
    services = app.state.services
    workflow = sequential_workflow()
    async with _client(app) as client:
        saved = await client.post("/api/workflows", json=workflow.model_dump(mode="json"))
        assert saved.status_code == 201, saved.text
        version = saved.json()["workflow_version"]
        response = await client.get(f"/api/workflows/{version}/graph")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mermaid"]
        assert all("conditional" in edge for edge in body["edges"])

        stored = services.repos.workflows.get(version)
        llm = NullLLM()
        assembly = assemble_workflow(
            settings=services.settings,
            repos=services.repos,
            workflow=stored,
            llm=llm,
            artifacts=services.artifacts,
            workspace=services.workspace,
            locks=services.locks,
            registry=services.registry,
            extensions=services.extensions,
        )
        drawable = compile_graph(assembly, checkpointer=None).get_graph()
        expected = {node_id for node_id in drawable.nodes if node_id not in _HIDDEN}
        assert {node["id"] for node in body["nodes"]} == expected
        missing = await client.get("/api/workflows/does-not-exist/graph")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_api_token_guards_api_but_not_health(settings: Settings, tmp_path: Path) -> None:
    settings = settings.model_copy(update={"data_dir": tmp_path / "auth", "api_token": "sekret"})
    app = create_app(settings, llm_factory=make_scripted_client)
    async with _client(app) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        denied = await client.get("/api/workflows")
        assert denied.status_code == 401
        assert denied.json()["code"] == "UNAUTHORIZED"
        allowed = await client.get("/api/workflows", headers={"Authorization": "Bearer sekret"})
        assert allowed.status_code == 200
