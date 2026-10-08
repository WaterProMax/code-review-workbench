"""Workspace service: uploads, immutable snapshots, candidate copies and patches.

Key guarantees (ImplementationPlan §6.2–6.3):
* uploads and published snapshots are immutable; edits only ever happen in a
  candidate directory that is published as a *new* version;
* patches use strict matching — a hunk that does not match is a failure, never a
  fuzzy overwrite;
* a candidate whose changed Python files do not parse is never published;
* publishing is idempotent: an already-published version is verified, not
  rewritten, so a crash between publish and bookkeeping is recoverable.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.artifacts import (
    FileEntry,
    Patch,
    PatchApplication,
    SourceManifest,
    StructuredEdit,
    UploadManifest,
    compute_source_version,
    normalise_relpath,
)
from app.schemas.enums import ArtifactType, PatchApplicationStatus, PatchFormat
from app.services.artifacts import ArtifactService, sha256_bytes
from app.settings import Settings
from app.storage.repositories import Repos


class DiffError(ValueError):
    """A patch could not be applied by strict matching."""


class PatchRejected(RuntimeError):
    """A patch is not allowed to touch this workspace (scope or test files)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Files a candidate patch may never modify: existing acceptance tests and the
# configuration that discovers them (§7.5.1, acceptance A28).
PROTECTED_TEST_BASENAMES = {
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
}
PROTECTED_TEST_DIRS = {"tests", "test"}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def is_protected_test_path(path: str) -> bool:
    rel = normalise_relpath(path)
    parts = rel.split("/")
    name = parts[-1]
    if name in PROTECTED_TEST_BASENAMES:
        return True
    if any(p in PROTECTED_TEST_DIRS for p in parts[:-1]):
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return False


# --------------------------------------------------------------------------- #
# unified diff
# --------------------------------------------------------------------------- #
@dataclass
class Hunk:
    old_start: int
    old_length: int
    new_length: int
    body: list[str]


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk]
    old_path: str | None = None


def _strip_ab_prefix(value: str) -> str:
    token = value.split("\t")[0].strip()
    if token.startswith("a/") or token.startswith("b/"):
        return token[2:]
    if token in ("/dev/null",):
        return token
    return token


def parse_unified_diff(diff_text: str) -> list[FilePatch]:
    lines = diff_text.split("\n")
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old = _strip_ab_prefix(line[4:])
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise DiffError("unified diff: '+++' must follow '---'")
            new = _strip_ab_prefix(lines[i + 1][4:])
            i += 2
            hunks: list[Hunk] = []
            while i < len(lines) and lines[i].startswith("@@"):
                match = _HUNK_RE.match(lines[i])
                if not match:
                    raise DiffError(f"unified diff: malformed hunk header {lines[i]!r}")
                old_start = int(match.group(1))
                old_len = int(match.group(2) or "1")
                new_len = int(match.group(4) or "1")
                i += 1
                body: list[str] = []
                seen_old = seen_new = 0
                while (seen_old < old_len or seen_new < new_len) and i < len(lines):
                    line = lines[i]
                    if line.startswith("\\"):
                        body.append(line)
                        i += 1
                        continue
                    if line.startswith("+"):
                        seen_new += 1
                    elif line.startswith("-"):
                        seen_old += 1
                    elif line.startswith(" ") or line == "":
                        seen_old += 1
                        seen_new += 1
                    else:
                        break
                    body.append(line)
                    i += 1
                if seen_old != old_len or seen_new != new_len:
                    raise DiffError(
                        f"unified diff: hunk at line {old_start} is truncated "
                        f"(expected -{old_len}/+{new_len}, got -{seen_old}/+{seen_new})"
                    )
                hunks.append(
                    Hunk(
                        old_start=old_start,
                        old_length=old_len,
                        new_length=new_len,
                        body=body,
                    )
                )
            if not hunks:
                raise DiffError(f"unified diff: no hunks for {new!r}")
            patches.append(FilePatch(path=new, hunks=hunks, old_path=old))
        else:
            i += 1
    if not patches:
        raise DiffError("unified diff: no file sections found")
    return patches


def apply_hunks(original: str, hunks: list[Hunk]) -> str:
    """Apply parsed hunks with strict, whitespace-sensitive matching."""
    had_trailing_newline = original.endswith("\n")
    lines = original.split("\n")
    if had_trailing_newline:
        lines.pop()

    offset = 0
    for hunk in hunks:
        old_lines: list[str] = []
        new_lines: list[str] = []
        for raw in hunk.body:
            if raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            if raw.startswith("+"):
                new_lines.append(raw[1:])
            elif raw.startswith("-"):
                old_lines.append(raw[1:])
            elif raw.startswith(" "):
                old_lines.append(raw[1:])
                new_lines.append(raw[1:])
            elif raw == "":
                old_lines.append("")
                new_lines.append("")
            else:
                raise DiffError(f"unified diff: unexpected hunk line {raw!r}")

        expected = hunk.old_start - 1 + offset
        position = _find_block(lines, old_lines, expected)
        if position is None:
            raise DiffError(
                f"hunk at line {hunk.old_start} does not match the file "
                "(strict matching; no fuzzy overwrite)"
            )
        lines[position : position + len(old_lines)] = new_lines
        offset += len(new_lines) - len(old_lines)

    text = "\n".join(lines)
    if had_trailing_newline:
        text += "\n"
    return text


def apply_unified_diff(original: str, diff_text: str) -> str:
    """Apply a single-file unified diff to ``original``."""
    file_patches = parse_unified_diff(diff_text)
    if len(file_patches) != 1:
        raise DiffError("apply_unified_diff expects exactly one file section")
    return apply_hunks(original, file_patches[0].hunks)


def _find_block(haystack: list[str], needle: list[str], expected: int) -> int | None:
    if not needle:
        if 0 <= expected <= len(haystack):
            return expected
        return None
    # try the exact position first, then nearby, then anywhere
    candidates = [expected]
    candidates.extend(range(max(0, expected - 40), min(len(haystack), expected + 41)))
    seen: set[int] = set()
    for index in candidates:
        if index in seen:
            continue
        seen.add(index)
        if index < 0 or index + len(needle) > len(haystack):
            continue
        if haystack[index : index + len(needle)] == needle:
            return index
    return None


def apply_structured_edits(original: str, edits: list[StructuredEdit]) -> str:
    """Apply exact-match replacements; every ``find`` must match verbatim."""
    text = original
    for edit in edits:
        count = text.count(edit.find)
        if count == 0:
            raise DiffError(
                f"structured edit for {edit.file_path!r} did not match any text "
                "(strict matching; no fuzzy overwrite)"
            )
        if edit.occurrence > count:
            raise DiffError(
                f"structured edit occurrence {edit.occurrence} exceeds {count} matches"
            )
        index = -1
        for _ in range(edit.occurrence):
            index = text.index(edit.find, index + 1)
        text = text[:index] + edit.replace + text[index + len(edit.find) :]
    return text


# --------------------------------------------------------------------------- #
# workspace service
# --------------------------------------------------------------------------- #
class WorkspaceService:
    def __init__(self, settings: Settings, artifacts: ArtifactService, repos: Repos) -> None:
        self.settings = settings
        self.artifacts = artifacts
        self.repos = repos

    # ---- paths -------------------------------------------------------------
    def upload_dir(self, source_id: str) -> Path:
        return self.settings.uploads_dir / normalise_relpath(source_id)

    def snapshot_dir(self, root_task_id: str, source_version: str) -> Path:
        return self.settings.snapshots_dir / root_task_id / source_version

    def candidate_dir(self, root_task_id: str, candidate_id: str) -> Path:
        return self.settings.workspaces_dir / root_task_id / candidate_id

    def patch_dir(self, root_task_id: str) -> Path:
        return self.settings.patches_dir / root_task_id

    # ---- uploads -----------------------------------------------------------
    def create_upload(self, files: list[tuple[str, bytes]]) -> UploadManifest:
        entries: list[FileEntry] = []
        for raw_path, blob in files:
            path = normalise_relpath(raw_path)
            entries.append(FileEntry(path=path, size=len(blob), sha256=sha256_bytes(blob)))
        manifest = UploadManifest(
            source_id=f"source-{uuid.uuid4().hex[:12]}",
            files=entries,
            total_bytes=sum(e.size for e in entries),
            content_digest=compute_source_version(entries),
        )
        dest = self.upload_dir(manifest.source_id)
        dest.mkdir(parents=True, exist_ok=True)
        for (raw_path, blob), entry in zip(files, entries):
            target = dest / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        self.repos.uploads.insert(
            source_id=manifest.source_id,
            content_digest=manifest.content_digest,
            files=[e.model_dump(mode="json") for e in manifest.files],
            total_bytes=manifest.total_bytes,
        )
        return manifest

    def read_upload(self, source_id: str) -> dict[str, bytes]:
        record = self.repos.uploads.get(source_id)
        if record is None:
            raise KeyError(f"unknown source {source_id}")
        base = self.upload_dir(source_id)
        return {
            entry["path"]: (base / entry["path"]).read_bytes() for entry in record["files"]
        }

    # ---- snapshots ---------------------------------------------------------
    def publish_snapshot(
        self,
        *,
        root_task_id: str,
        files: dict[str, bytes],
        parent_version: str | None = None,
        patch_id: str | None = None,
        created_by_attempt_id: str | None = None,
    ) -> SourceManifest:
        normalised = {normalise_relpath(p): b for p, b in files.items()}
        entries = [
            FileEntry(path=p, size=len(b), sha256=sha256_bytes(b))
            for p, b in sorted(normalised.items())
        ]
        manifest = SourceManifest.build(
            entries,
            parent_version=parent_version,
            patch_id=patch_id,
            created_by_attempt_id=created_by_attempt_id,
        )
        dest = self.snapshot_dir(root_task_id, manifest.source_version)
        if dest.exists():
            existing = self.resolve_manifest(root_task_id, manifest.source_version)
            if existing.files != manifest.files:
                raise RuntimeError(
                    f"snapshot {manifest.source_version} exists with different content"
                )
            return existing

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".tmp-{uuid.uuid4().hex[:8]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            for path, blob in normalised.items():
                target = tmp / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            (tmp / ".snapshot.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8"
            )
            with self.repos.db.transaction():
                os.replace(tmp, dest)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        return manifest

    def publish_initial_snapshot(
        self,
        *,
        root_task_id: str,
        source_id: str,
        created_by_attempt_id: str | None = None,
    ) -> SourceManifest:
        """Freeze the uploaded input as the first immutable snapshot of a task."""
        return self.publish_snapshot(
            root_task_id=root_task_id,
            files=self.read_upload(source_id),
            created_by_attempt_id=created_by_attempt_id,
        )

    def resolve_manifest(self, root_task_id: str, source_version: str) -> SourceManifest:
        meta = self.snapshot_dir(root_task_id, source_version) / ".snapshot.json"
        if not meta.exists():
            raise KeyError(f"unknown snapshot {source_version} for {root_task_id}")
        return SourceManifest.model_validate_json(meta.read_text(encoding="utf-8"))

    def snapshot_exists(self, root_task_id: str, source_version: str) -> bool:
        return (self.snapshot_dir(root_task_id, source_version) / ".snapshot.json").exists()

    def list_files(self, root_task_id: str, source_version: str) -> list[str]:
        return sorted(entry.path for entry in self.resolve_manifest(root_task_id, source_version).files)

    def file_path(self, root_task_id: str, source_version: str, relpath: str) -> Path:
        base = self.snapshot_dir(root_task_id, source_version)
        path = (base / normalise_relpath(relpath)).resolve()
        if not str(path).startswith(str(base.resolve()) + "/"):
            raise ValueError(f"path escapes the snapshot: {relpath}")
        return path

    def read_file(self, root_task_id: str, source_version: str, relpath: str) -> bytes:
        path = self.file_path(root_task_id, source_version, relpath)
        if not path.exists():
            raise KeyError(f"{relpath} is not part of snapshot {source_version}")
        return path.read_bytes()

    def read_text(self, root_task_id: str, source_version: str, relpath: str) -> str:
        return self.read_file(root_task_id, source_version, relpath).decode("utf-8")

    def snapshot_files(self, root_task_id: str, source_version: str) -> dict[str, bytes]:
        return {
            rel: self.read_file(root_task_id, source_version, rel)
            for rel in self.list_files(root_task_id, source_version)
        }

    def register_source_artifact(
        self,
        *,
        root_task_id: str,
        source_version: str,
        producer_attempt_id: str | None = None,
    ) -> str:
        """Register the snapshot manifest file as a source_snapshot artifact."""
        manifest_path = self.snapshot_dir(root_task_id, source_version) / ".snapshot.json"
        artifact = self.artifacts.register_file(
            root_task_id=root_task_id,
            artifact_type=ArtifactType.SOURCE_SNAPSHOT,
            path=manifest_path,
            producer_attempt_id=producer_attempt_id,
            source_version=source_version,
            metadata={"source_version": source_version},
        )
        return artifact.artifact_id

    # ---- candidates --------------------------------------------------------
    def create_candidate(self, root_task_id: str, base_version: str) -> tuple[str, Path]:
        candidate_id = f"cand-{uuid.uuid4().hex[:12]}"
        dest = self.candidate_dir(root_task_id, candidate_id)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for path, blob in self.snapshot_files(root_task_id, base_version).items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return candidate_id, dest

    # ---- patch application -------------------------------------------------
    def apply_patch(
        self,
        *,
        root_task_id: str,
        patch_artifact_id: str,
        repair_attempt_id: str,
        base_version: str,
        idempotency_key: str,
    ) -> PatchApplication:
        """Apply a patch following the six ordered steps of §6.3."""
        patch = Patch.model_validate(self.artifacts.read_json(patch_artifact_id))
        if patch.root_task_id != root_task_id:
            raise PatchRejected(
                "PATCH_FOREIGN", "patch belongs to a different root task"
            )
        if patch.base_version != base_version:
            raise PatchRejected(
                "PATCH_CONFLICT",
                f"patch targets {patch.base_version} but the current version is {base_version}",
            )

        attempt = self.repos.attempts.get(repair_attempt_id)
        if attempt is None or attempt.root_task_id != root_task_id:
            raise PatchRejected("PATCH_FOREIGN", "repair attempt does not belong to this task")
        owner_task = self.repos.tasks.get(attempt.task_id)
        if owner_task is None or owner_task.task_kind != "fix":
            raise PatchRejected("PATCH_FOREIGN", "only a fix attempt may publish a patch")

        target_paths = _patch_paths(patch)
        if not target_paths:
            raise PatchRejected("PATCH_INVALID", "patch does not target any file")
        manifest = self.resolve_manifest(root_task_id, base_version)
        known = {entry.path for entry in manifest.files}
        unknown = sorted(p for p in target_paths if p not in known)
        if unknown:
            raise PatchRejected(
                "PATCH_INVALID", f"patch targets files outside the snapshot: {unknown}"
            )
        protected = sorted(p for p in target_paths if is_protected_test_path(p))
        if protected:
            raise PatchRejected(
                "PATCH_PROTECTED_TEST",
                f"a candidate patch may not modify acceptance tests or test config: {protected}",
            )

        existing = self.repos.patch_applications.get_by_key(idempotency_key)
        if existing is not None and existing.status is PatchApplicationStatus.COMMITTED:
            return existing
        application_id = existing.application_id if existing else f"pa-{uuid.uuid4().hex[:12]}"

        candidate_id, candidate = self.create_candidate(root_task_id, base_version)
        try:
            changed = self._apply_to_candidate(candidate, patch)
            self._syntax_check(candidate, changed)
            files = {
                rel: (candidate / rel).read_bytes()
                for rel in self.list_files(root_task_id, base_version)
            }
        except (DiffError, SyntaxError, UnicodeDecodeError) as exc:
            evidence = self.artifacts.save_text(
                root_task_id=root_task_id,
                artifact_type=ArtifactType.EVIDENCE,
                text=f"patch {patch.patch_id} could not be applied to {base_version}:\n{exc}\n",
                producer_attempt_id=repair_attempt_id,
                source_version=base_version,
                name=f"patch-failure-{application_id}",
            )
            failure = PatchApplication(
                application_id=application_id,
                root_task_id=root_task_id,
                patch_id=patch.patch_id,
                repair_attempt_id=repair_attempt_id,
                base_version=base_version,
                status=PatchApplicationStatus.FAILED,
                idempotency_key=idempotency_key,
                error=f"{type(exc).__name__}: {exc}",
            )
            if existing is None:
                self.repos.patch_applications.insert(failure)
            else:
                self.repos.patch_applications.update(
                    application_id, status=PatchApplicationStatus.FAILED, error=failure.error
                )
            return failure
        finally:
            shutil.rmtree(candidate, ignore_errors=True)

        # The result version is content-addressed and known before publishing, so
        # the intent can be recorded first: a crash between the file publish and the
        # commit is then recoverable without applying the patch a second time (§11.2).
        manifest_new = SourceManifest.build(
            _file_entries(files),
            parent_version=base_version,
            patch_id=patch.patch_id,
            created_by_attempt_id=repair_attempt_id,
        )
        if manifest_new.source_version == base_version:
            raise PatchRejected(
                "PATCH_NO_CHANGE",
                "patch produced no change; refusing to publish an identical version",
            )

        intent = PatchApplication(
            application_id=application_id,
            root_task_id=root_task_id,
            patch_id=patch.patch_id,
            repair_attempt_id=repair_attempt_id,
            base_version=base_version,
            result_version=manifest_new.source_version,
            status=PatchApplicationStatus.PREPARED,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            self.repos.patch_applications.insert(intent)
        else:
            self.repos.patch_applications.update(
                application_id,
                status=PatchApplicationStatus.PREPARED,
                result_version=manifest_new.source_version,
            )

        published = self.publish_snapshot(
            root_task_id=root_task_id,
            files=files,
            parent_version=base_version,
            patch_id=patch.patch_id,
            created_by_attempt_id=repair_attempt_id,
        )
        assert published.source_version == manifest_new.source_version

        return self.commit_application(intent, committed_at=_now())

    def commit_application(
        self, application: PatchApplication, *, committed_at: datetime | None = None
    ) -> PatchApplication:
        """Mark a prepared application committed after its snapshot is published."""
        committed = application.model_copy(
            update={
                "status": PatchApplicationStatus.COMMITTED,
                "committed_at": committed_at or _now(),
                "error": None,
            }
        )
        self.repos.patch_applications.update(
            application.application_id,
            status=PatchApplicationStatus.COMMITTED,
            result_version=committed.result_version,
            committed_at=committed.committed_at,
        )
        self.artifacts.save_json(
            root_task_id=application.root_task_id,
            artifact_type=ArtifactType.PATCH_APPLICATION,
            data=committed.model_dump(mode="json"),
            producer_attempt_id=application.repair_attempt_id,
            source_version=committed.result_version,
            name=f"application-{application.application_id}",
        )
        return committed

    def verify_published_application(self, application: PatchApplication) -> bool:
        """Whether the snapshot this application claims to have published exists.

        A published snapshot is content-addressed, so its existence (and the
        metadata that binds it to this patch and base version) is enough to
        confirm the side effect happened without replaying the patch (§11.2).
        """
        if not application.result_version:
            return False
        if not self.snapshot_exists(application.root_task_id, application.result_version):
            return False
        manifest = self.resolve_manifest(application.root_task_id, application.result_version)
        return (
            manifest.parent_version == application.base_version
            and manifest.patch_id == application.patch_id
        )

    def _apply_to_candidate(self, candidate: Path, patch: Patch) -> list[str]:
        changed: list[str] = []
        if patch.format is PatchFormat.UNIFIED_DIFF:
            for file_patch in parse_unified_diff(patch.diff_text or ""):
                rel = normalise_relpath(file_patch.path)
                target = candidate / rel
                if not target.exists():
                    raise DiffError(f"patch targets a missing file: {rel}")
                original = target.read_text(encoding="utf-8")
                target.write_text(apply_hunks(original, file_patch.hunks), encoding="utf-8")
                changed.append(rel)
            return changed

        by_file: dict[str, list[StructuredEdit]] = {}
        for edit in patch.edits:
            by_file.setdefault(edit.file_path, []).append(edit)
        for rel, edits in by_file.items():
            target = candidate / rel
            if not target.exists():
                raise DiffError(f"patch targets a missing file: {rel}")
            original = target.read_text(encoding="utf-8")
            target.write_text(apply_structured_edits(original, edits), encoding="utf-8")
            changed.append(rel)
        return changed

    @staticmethod
    def _syntax_check(candidate: Path, changed: list[str]) -> None:
        for rel in changed:
            if not rel.endswith(".py"):
                continue
            text = (candidate / rel).read_text(encoding="utf-8")
            try:
                ast.parse(text, filename=rel)
            except SyntaxError as exc:
                raise SyntaxError(f"{rel}:{exc.lineno}: {exc.msg}") from exc

def _patch_paths(patch: Patch) -> set[str]:
    if patch.format is PatchFormat.UNIFIED_DIFF and patch.diff_text:
        return {normalise_relpath(fp.path) for fp in parse_unified_diff(patch.diff_text)}
    return {normalise_relpath(e.file_path) for e in patch.edits}


def _file_entries(files: dict[str, bytes]) -> list[FileEntry]:
    return [
        FileEntry(path=p, size=len(b), sha256=sha256_bytes(b))
        for p, b in sorted(files.items())
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)
