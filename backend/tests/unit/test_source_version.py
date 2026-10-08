"""P01: source-version determinism and workspace-path safety (§5.2.5, §6.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.artifacts import (
    FileEntry,
    SourceManifest,
    UploadManifest,
    compute_source_version,
    normalise_relpath,
)


def _entry(path: str, content: bytes) -> FileEntry:
    import hashlib

    return FileEntry(path=path, size=len(content), sha256=hashlib.sha256(content).hexdigest())


def test_same_inputs_give_same_version() -> None:
    a = _entry("pkg/a.py", b"print(1)\n")
    b = _entry("pkg/b.py", b"x = 2\n")
    assert compute_source_version([a, b]) == compute_source_version([b, a])


def test_order_is_irrelevant_but_content_is_not() -> None:
    a = _entry("a.py", b"print(1)\n")
    b = _entry("b.py", b"print(2)\n")
    changed = _entry("b.py", b"print(3)\n")
    assert compute_source_version([a, b]) == compute_source_version([b, a])
    assert compute_source_version([a, b]) != compute_source_version([a, changed])


def test_path_normalisation_is_stable() -> None:
    assert normalise_relpath("pkg/./a.py") == "pkg/a.py"
    assert normalise_relpath("pkg//a.py") == "pkg/a.py"
    assert normalise_relpath("pkg\\a.py") == "pkg/a.py"
    assert compute_source_version([_entry("pkg/a.py", b"x")]) == compute_source_version(
        [_entry("pkg/./a.py", b"x")]
    )


@pytest.mark.parametrize("bad", ["/etc/hosts", "../secret.py", "a/../../b.py", "", "C:/x.py"])
def test_unsafe_paths_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        normalise_relpath(bad)


def test_parent_version_changes_the_digest() -> None:
    entry = _entry("a.py", b"x = 1\n")
    assert compute_source_version([entry]) != compute_source_version([entry], "sv-parent")


def test_source_manifest_rejects_tampered_version() -> None:
    entries = [_entry("a.py", b"x = 1\n")]
    manifest = SourceManifest.build(entries)
    assert manifest.source_version == compute_source_version(entries)
    with pytest.raises(ValidationError):
        SourceManifest(source_version="sv-deadbeef", files=entries)


def test_manifest_records_parent_and_patch_lineage() -> None:
    entries = [_entry("a.py", b"x = 2\n")]
    manifest = SourceManifest.build(entries, parent_version="sv-parent", patch_id="P-1")
    assert manifest.parent_version == "sv-parent"
    assert manifest.patch_id == "P-1"


def test_upload_manifest_requires_unique_paths_and_matching_digest() -> None:
    entries = [_entry("a.py", b"x = 1\n")]
    good = UploadManifest(
        source_id="source-1",
        files=entries,
        total_bytes=entries[0].size,
        content_digest=compute_source_version(entries),
    )
    assert good.source_id == "source-1"

    with pytest.raises(ValidationError):
        UploadManifest(
            source_id="source-1",
            files=[entries[0], entries[0]],
            total_bytes=entries[0].size * 2,
            content_digest=compute_source_version(entries),
        )

    with pytest.raises(ValidationError):
        UploadManifest(
            source_id="source-1",
            files=entries,
            total_bytes=entries[0].size,
            content_digest="sv-wrong",
        )


def test_source_version_is_a_content_id_not_a_table_reference() -> None:
    """The version must be derivable from content alone (no version table)."""
    entries = [_entry("a.py", b"x = 1\n")]
    first = compute_source_version(entries)
    second = compute_source_version(list(entries))
    assert first == second
    assert first.startswith("sv-")
