"""Artifact service: the only path for reading/writing evidence files (§6.2).

Files live under the configured data directory and are addressed by a
``storage_ref`` relative to it. Every read is resolved and checked to stay inside
the data directory, so no caller can read an arbitrary server path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas.artifacts import Artifact, normalise_relpath
from app.schemas.enums import ArtifactType
from app.settings import Settings
from app.storage.repositories import ArtifactRepository


class ArtifactError(RuntimeError):
    pass


_EXT = {
    ArtifactType.SOURCE_SNAPSHOT: ".json",
    ArtifactType.UPLOAD_MANIFEST: ".json",
    ArtifactType.ACCEPTANCE_CONTRACT: ".json",
    ArtifactType.FINDING: ".json",
    ArtifactType.PATCH: ".json",
    ArtifactType.PATCH_APPLICATION: ".json",
    ArtifactType.VERIFICATION_REPORT: ".json",
    ArtifactType.CHECK_RESULT: ".json",
    ArtifactType.FINAL_REPORT: ".json",
    ArtifactType.REPORT_MARKDOWN: ".md",
    ArtifactType.TEST_ARTIFACT: ".py",
    ArtifactType.DETECTION: ".json",
    ArtifactType.EVIDENCE: ".txt",
    ArtifactType.DIFF: ".diff",
    ArtifactType.MODEL_OUTPUT: ".json",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactService:
    def __init__(self, settings: Settings, repo: ArtifactRepository) -> None:
        self.settings = settings
        self.repo = repo

    # ---- ids / paths -------------------------------------------------------
    @staticmethod
    def new_id(prefix: str = "art") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    @property
    def root(self) -> Path:
        return self.settings.data_dir

    def _rel(self, *parts: str) -> str:
        return normalise_relpath("/".join(p.strip("/") for p in parts if p))

    def resolve_path(self, storage_ref: str, must_exist: bool = False) -> Path:
        rel = normalise_relpath(storage_ref)
        path = (self.root / rel).resolve()
        root = self.root.resolve()
        if not str(path).startswith(str(root) + "/") and path != root:
            raise ArtifactError(f"artifact path escapes the data dir: {storage_ref}")
        if must_exist and not path.exists():
            raise ArtifactError(f"artifact file missing: {storage_ref}")
        return path

    # ---- writes ------------------------------------------------------------
    def save_json(
        self,
        *,
        root_task_id: str,
        artifact_type: ArtifactType,
        data: Any,
        producer_attempt_id: str | None = None,
        source_version: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
        artifact_id: str | None = None,
        subdir: str | None = None,
    ) -> Artifact:
        blob = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        return self._write(
            root_task_id=root_task_id,
            artifact_type=artifact_type,
            blob=blob,
            producer_attempt_id=producer_attempt_id,
            source_version=source_version,
            name=name,
            metadata=metadata,
            artifact_id=artifact_id,
            subdir=subdir,
        )

    def save_text(
        self,
        *,
        root_task_id: str,
        artifact_type: ArtifactType,
        text: str,
        producer_attempt_id: str | None = None,
        source_version: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
        artifact_id: str | None = None,
        subdir: str | None = None,
    ) -> Artifact:
        return self._write(
            root_task_id=root_task_id,
            artifact_type=artifact_type,
            blob=text.encode("utf-8"),
            producer_attempt_id=producer_attempt_id,
            source_version=source_version,
            name=name,
            metadata=metadata,
            artifact_id=artifact_id,
            subdir=subdir,
        )

    def register_file(
        self,
        *,
        root_task_id: str,
        artifact_type: ArtifactType,
        path: Path,
        producer_attempt_id: str | None = None,
        source_version: str | None = None,
        metadata: dict | None = None,
        artifact_id: str | None = None,
    ) -> Artifact:
        """Register a file already written under the data dir (e.g. a snapshot)."""
        path = path.resolve()
        rel = path.relative_to(self.root.resolve()).as_posix()
        blob = path.read_bytes()
        artifact = Artifact(
            artifact_id=artifact_id or self.new_id(_prefix(artifact_type)),
            root_task_id=root_task_id,
            producer_attempt_id=producer_attempt_id,
            artifact_type=artifact_type,
            source_version=source_version,
            storage_ref=rel,
            hash=sha256_bytes(blob),
            size=len(blob),
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
        )
        self.repo.insert(artifact)
        return artifact

    def _write(
        self,
        *,
        root_task_id: str,
        artifact_type: ArtifactType,
        blob: bytes,
        producer_attempt_id: str | None,
        source_version: str | None,
        name: str | None,
        metadata: dict | None,
        artifact_id: str | None,
        subdir: str | None,
    ) -> Artifact:
        aid = artifact_id or self.new_id(_prefix(artifact_type))
        ext = _EXT.get(artifact_type, ".bin")
        filename = f"{name}-{aid}{ext}" if name else f"{aid}{ext}"
        rel = self._rel("evidence", root_task_id, subdir or artifact_type.value, filename)
        path = self.resolve_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        artifact = Artifact(
            artifact_id=aid,
            root_task_id=root_task_id,
            producer_attempt_id=producer_attempt_id,
            artifact_type=artifact_type,
            source_version=source_version,
            storage_ref=rel,
            hash=sha256_bytes(blob),
            size=len(blob),
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
        )
        self.repo.insert(artifact)
        return artifact

    # ---- reads -------------------------------------------------------------
    def get(self, artifact_id: str) -> Artifact:
        artifact = self.repo.get(artifact_id)
        if artifact is None:
            raise ArtifactError(f"unknown artifact {artifact_id}")
        return artifact

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        path = self.resolve_path(artifact.storage_ref, must_exist=True)
        data = path.read_bytes()
        if sha256_bytes(data) != artifact.hash:
            raise ArtifactError(f"artifact {artifact_id} content hash mismatch")
        return data

    def read_json(self, artifact_id: str) -> Any:
        return json.loads(self.read_bytes(artifact_id).decode("utf-8"))

    def read_text(self, artifact_id: str) -> str:
        return self.read_bytes(artifact_id).decode("utf-8")

    def verify_ownership(self, artifact_id: str, root_task_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact.root_task_id != root_task_id:
            raise ArtifactError(
                f"artifact {artifact_id} belongs to another root task"
            )
        return artifact

    def list_by_root(self, root_task_id: str, artifact_type: ArtifactType | None = None) -> list[Artifact]:
        return self.repo.list_by_root(
            root_task_id, artifact_type.value if artifact_type else None
        )


_PREFIX = {
    ArtifactType.SOURCE_SNAPSHOT: "snapshot",
    ArtifactType.UPLOAD_MANIFEST: "upload",
    ArtifactType.ACCEPTANCE_CONTRACT: "contract",
    ArtifactType.FINDING: "findings",
    ArtifactType.PATCH: "patch",
    ArtifactType.PATCH_APPLICATION: "patchapp",
    ArtifactType.VERIFICATION_REPORT: "verification",
    ArtifactType.CHECK_RESULT: "check",
    ArtifactType.FINAL_REPORT: "report",
    ArtifactType.REPORT_MARKDOWN: "reportmd",
    ArtifactType.TEST_ARTIFACT: "tests",
    ArtifactType.DETECTION: "detection",
    ArtifactType.EVIDENCE: "evidence",
    ArtifactType.DIFF: "diff",
    ArtifactType.MODEL_OUTPUT: "model",
}


def _prefix(artifact_type: ArtifactType) -> str:
    return _PREFIX.get(artifact_type, "art")
