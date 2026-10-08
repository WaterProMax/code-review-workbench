"""Artifact, snapshot, patch and upload contracts (§5, §6.2–6.3)."""

from __future__ import annotations

import hashlib
import json
import posixpath
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import utcnow
from app.schemas.enums import ArtifactType, PatchApplicationStatus, PatchFormat


class FileEntry(BaseModel):
    """One file inside a manifest, identified by normalised relative path."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)

    @field_validator("path")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_relpath(value)


def normalise_relpath(value: str) -> str:
    """Reject absolute paths and traversal; collapse to a POSIX relative path."""
    if not value or value in (".", ".."):
        raise ValueError("empty or invalid path")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError(f"absolute path not allowed: {value!r}")
    if "\\" in value:
        value = value.replace("\\", "/")
    if ":" in value.split("/")[0]:
        raise ValueError(f"drive-qualified path not allowed: {value!r}")
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"path traversal not allowed: {value!r}")
    normalised = posixpath.normpath("/".join(parts))
    if normalised.startswith("..") or "/../" in normalised:
        raise ValueError(f"path escapes the workspace: {value!r}")
    return normalised


def compute_source_version(entries: list[FileEntry], parent_version: str | None = None) -> str:
    """Deterministic content version: sorted normalised manifest + content hashes.

    Encoding, ordering and delimiter are fixed so identical inputs always
    produce the same version (plan §5.2.5).
    """
    ordered = sorted(entries, key=lambda e: e.path)
    blob = "\n".join(f"{e.path}\0{e.size}\0{e.sha256}" for e in ordered)
    if parent_version:
        blob = f"parent:{parent_version}\n" + blob
    return "sv-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class UploadManifest(BaseModel):
    """Immutable record of what a user uploaded (never a task id)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    files: list[FileEntry] = Field(min_length=1)
    total_bytes: int = Field(ge=0)
    content_digest: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> "UploadManifest":
        paths = [f.path for f in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate paths in upload manifest")
        if self.total_bytes != sum(f.size for f in self.files):
            raise ValueError("total_bytes must equal the sum of file sizes")
        if self.content_digest != compute_source_version(self.files):
            raise ValueError("content_digest does not match the file list")
        return self


class SourceManifest(BaseModel):
    """Content of a frozen snapshot; the source of ``source_version``."""

    model_config = ConfigDict(extra="forbid")

    source_version: str = Field(min_length=1)
    files: list[FileEntry] = Field(min_length=1)
    parent_version: str | None = None
    patch_id: str | None = None
    created_by_attempt_id: str | None = None
    frozen_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> "SourceManifest":
        if self.source_version != compute_source_version(self.files, self.parent_version):
            raise ValueError("source_version does not match the frozen manifest")
        return self

    @staticmethod
    def build(
        files: list[FileEntry],
        parent_version: str | None = None,
        patch_id: str | None = None,
        created_by_attempt_id: str | None = None,
    ) -> "SourceManifest":
        return SourceManifest(
            source_version=compute_source_version(files, parent_version),
            files=sorted(files, key=lambda f: f.path),
            parent_version=parent_version,
            patch_id=patch_id,
            created_by_attempt_id=created_by_attempt_id,
        )


class GeneratedTest(BaseModel):
    """One minimal reproduction test produced for a specific check."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content: str = Field(min_length=1)
    expected_source: str = Field(min_length=1, description="where the expectation comes from")


class GeneratedTestSuite(BaseModel):
    """Payload of a ``test_artifact`` bundle (P10 extension sample).

    Produced by the test-generation role and consumed as a verifier input, so the
    extension's artifact is a real input dependency rather than a side file.
    """

    model_config = ConfigDict(extra="forbid")

    producer_attempt_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    target_finding_ids: list[str] = Field(default_factory=list)
    tests: list[GeneratedTest] = Field(min_length=1)
    notes: str | None = None

    def by_check(self) -> dict[str, list["GeneratedTest"]]:
        out: dict[str, list[GeneratedTest]] = {}
        for test in self.tests:
            out.setdefault(test.check_id, []).append(test)
        return out


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    producer_attempt_id: str | None = None
    artifact_type: ArtifactType
    source_version: str | None = None
    storage_ref: str = Field(min_length=1, description="path relative to the data dir")
    hash: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("storage_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return normalise_relpath(value)


class StructuredEdit(BaseModel):
    """A single exact-match replacement (no fuzzy overwrite allowed)."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1)
    find: str = Field(min_length=1)
    replace: str
    occurrence: int = Field(default=1, ge=1)

    @field_validator("file_path")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return normalise_relpath(value)


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    producer_attempt_id: str = Field(min_length=1)
    base_version: str = Field(min_length=1)
    target_finding_ids: list[str] = Field(default_factory=list)
    format: PatchFormat = PatchFormat.UNIFIED_DIFF
    diff_text: str | None = None
    edits: list[StructuredEdit] = Field(default_factory=list)
    rationale: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_payload(self) -> "Patch":
        if self.format is PatchFormat.UNIFIED_DIFF:
            if not self.diff_text:
                raise ValueError("unified_diff patch requires diff_text")
            if self.edits:
                raise ValueError("unified_diff patch must not carry structured edits")
        else:
            if not self.edits:
                raise ValueError("structured_edits patch requires at least one edit")
            if self.diff_text:
                raise ValueError("structured_edits patch must not carry a diff")
        return self

    def fingerprint(self) -> str:
        payload = {
            "base_version": self.base_version,
            "format": self.format.value,
            "diff_text": self.diff_text,
            "edits": [e.model_dump() for e in self.edits],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


class PatchApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    patch_id: str = Field(min_length=1)
    repair_attempt_id: str = Field(min_length=1)
    base_version: str = Field(min_length=1)
    result_version: str | None = None
    status: PatchApplicationStatus = PatchApplicationStatus.PREPARED
    idempotency_key: str = Field(min_length=1)
    error: str | None = None
    prepared_at: datetime = Field(default_factory=utcnow)
    committed_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> "PatchApplication":
        if self.status is PatchApplicationStatus.COMMITTED:
            if not self.result_version:
                raise ValueError("committed application requires result_version")
            if self.result_version == self.base_version:
                raise ValueError("result_version must differ from base_version")
            if self.error:
                raise ValueError("committed application must not carry an error")
        if self.status is PatchApplicationStatus.FAILED and not self.error:
            raise ValueError("failed application requires an error")
        return self
