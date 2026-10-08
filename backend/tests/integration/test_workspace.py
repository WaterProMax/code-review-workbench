"""P02 acceptance: snapshots, work copies, patch publishing and artifacts.

Covers "multi-file snapshot versions are stable", "the original input is not
modified", "invalid patches are not published" and "repeating a patch
application is idempotent" (ImplementationPlan §6 验收, A07/A14/A19).
"""

from __future__ import annotations

import pytest

from app.schemas.artifacts import Patch, StructuredEdit
from app.schemas.enums import ArtifactType, PatchApplicationStatus
from app.services.artifacts import ArtifactError, ArtifactService
from app.services.workspace import PatchRejected, WorkspaceService
from app.storage.repositories import Repos
from tests.fixtures import builders

BASE_SOURCE = {
    "helpers.py": (
        "def add_item(item, items=[]):\n"
        "    items.append(item)\n"
        "    return items\n"
        "\n"
        "\n"
        "def average(values):\n"
        "    return sum(values) / len(values)\n"
    ),
    "pkg/extra.py": "VALUE = 1\n",
}


def _setup(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> tuple[str, str]:
    root = builders.new_root(repos)
    fix = builders.new_child(repos, root, task_id="T102", task_kind="fix")
    builders.new_attempt(repos, root, fix, attempt_id="T102-A1", batch_id="B1", source_version="sv-x")
    upload = workspace.create_upload([(path, body.encode()) for path, body in BASE_SOURCE.items()])
    manifest = workspace.publish_initial_snapshot(root_task_id=root.task_id, source_id=upload.source_id)
    workspace.register_source_artifact(root_task_id=root.task_id, source_version=manifest.source_version)
    return root.task_id, manifest.source_version


def test_upload_and_snapshot_are_immutable_and_order_independent(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, version = _setup(workspace, repos, artifacts)
    assert workspace.list_files(root_task_id, version) == ["helpers.py", "pkg/extra.py"]

    # identical content, different submission order -> identical version
    again = workspace.create_upload(
        [(path, body.encode()) for path, body in reversed(list(BASE_SOURCE.items()))]
    )
    manifest2 = workspace.publish_snapshot(root_task_id=root_task_id, files=workspace.read_upload(again.source_id))
    assert manifest2.source_version == version


def test_patch_publishes_a_new_version_and_leaves_the_base_untouched(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    patch = Patch(
        patch_id="P-1",
        root_task_id=root_task_id,
        producer_attempt_id="T102-A1",
        base_version=base,
        target_finding_ids=["F-1"],
        format="structured_edits",
        edits=[
            StructuredEdit(
                file_path="helpers.py",
                find="def add_item(item, items=[]):\n    items.append(item)\n    return items\n",
                replace=(
                    "def add_item(item, items=None):\n"
                    "    if items is None:\n"
                    "        items = []\n"
                    "    items.append(item)\n"
                    "    return items\n"
                ),
            )
        ],
        rationale="avoid a shared mutable default",
    )
    patch_artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.PATCH,
        data=patch.model_dump(mode="json"),
        producer_attempt_id="T102-A1",
        source_version=base,
    )

    application = workspace.apply_patch(
        root_task_id=root_task_id,
        patch_artifact_id=patch_artifact.artifact_id,
        repair_attempt_id="T102-A1",
        base_version=base,
        idempotency_key="apply:P-1",
    )
    assert application.status is PatchApplicationStatus.COMMITTED
    assert application.result_version and application.result_version != base

    # the new version has the fix; the base snapshot is unchanged
    assert "items=None" in workspace.read_text(root_task_id, application.result_version, "helpers.py")
    assert "items=[]" in workspace.read_text(root_task_id, base, "helpers.py")

    parent = workspace.resolve_manifest(root_task_id, application.result_version)
    assert parent.parent_version == base
    assert parent.patch_id == "P-1"

    # repeating the same application is idempotent
    repeat = workspace.apply_patch(
        root_task_id=root_task_id,
        patch_artifact_id=patch_artifact.artifact_id,
        repair_attempt_id="T102-A1",
        base_version=base,
        idempotency_key="apply:P-1",
    )
    assert repeat.result_version == application.result_version
    assert len(workspace.list_files(root_task_id, base)) == 2


def test_patch_with_syntax_error_is_never_published(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    bad = Patch(
        patch_id="P-bad",
        root_task_id=root_task_id,
        producer_attempt_id="T102-A1",
        base_version=base,
        format="structured_edits",
        edits=[StructuredEdit(file_path="helpers.py", find="return items", replace="return items(")],
        rationale="broken",
    )
    artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.PATCH,
        data=bad.model_dump(mode="json"),
        producer_attempt_id="T102-A1",
    )
    application = workspace.apply_patch(
        root_task_id=root_task_id,
        patch_artifact_id=artifact.artifact_id,
        repair_attempt_id="T102-A1",
        base_version=base,
        idempotency_key="apply:P-bad",
    )
    assert application.status is PatchApplicationStatus.FAILED
    assert application.result_version is None
    assert "SyntaxError" in (application.error or "")
    # no new snapshot was published
    assert workspace.list_files(root_task_id, base) == ["helpers.py", "pkg/extra.py"]


def test_patch_targeting_tests_is_rejected(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    weaken = Patch(
        patch_id="P-test",
        root_task_id=root_task_id,
        producer_attempt_id="T102-A1",
        base_version=base,
        format="structured_edits",
        edits=[StructuredEdit(file_path="tests/test_helpers.py", find="assert x", replace="pass")],
        rationale="weaken the acceptance test",
    )
    artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.PATCH,
        data=weaken.model_dump(mode="json"),
        producer_attempt_id="T102-A1",
    )
    with pytest.raises(PatchRejected) as exc:
        workspace.apply_patch(
            root_task_id=root_task_id,
            patch_artifact_id=artifact.artifact_id,
            repair_attempt_id="T102-A1",
            base_version=base,
            idempotency_key="apply:P-test",
        )
    assert exc.value.code in {"PATCH_PROTECTED_TEST", "PATCH_INVALID"}


def test_patch_for_a_stale_base_version_is_rejected(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    patch = Patch(
        patch_id="P-stale",
        root_task_id=root_task_id,
        producer_attempt_id="T102-A1",
        base_version="sv-outdated",
        format="structured_edits",
        edits=[StructuredEdit(file_path="helpers.py", find="return items", replace="return items")],
        rationale="stale",
    )
    artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.PATCH,
        data=patch.model_dump(mode="json"),
        producer_attempt_id="T102-A1",
    )
    with pytest.raises(PatchRejected) as exc:
        workspace.apply_patch(
            root_task_id=root_task_id,
            patch_artifact_id=artifact.artifact_id,
            repair_attempt_id="T102-A1",
            base_version=base,
            idempotency_key="apply:P-stale",
        )
    assert exc.value.code == "PATCH_CONFLICT"


def test_artifact_reads_are_confined_and_ownership_is_checked(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService, settings
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.EVIDENCE,
        data={"ok": True},
    )
    assert artifacts.read_json(artifact.artifact_id) == {"ok": True}
    assert artifacts.verify_ownership(artifact.artifact_id, root_task_id).artifact_id == artifact.artifact_id

    with pytest.raises(ArtifactError):
        artifacts.verify_ownership(artifact.artifact_id, "T999")
    with pytest.raises(Exception):
        artifacts.resolve_path("../../etc/passwd")

    # evidence files live inside the data dir
    assert str(settings.data_dir) in str(artifacts.resolve_path(artifact.storage_ref, must_exist=True))


def test_snapshot_artifact_metadata_links_version_and_producer(
    workspace: WorkspaceService, repos: Repos, artifacts: ArtifactService
) -> None:
    root_task_id, base = _setup(workspace, repos, artifacts)
    listed = artifacts.list_by_root(root_task_id, ArtifactType.SOURCE_SNAPSHOT)
    assert len(listed) == 1
    assert listed[0].source_version == base
    assert listed[0].metadata["source_version"] == base
