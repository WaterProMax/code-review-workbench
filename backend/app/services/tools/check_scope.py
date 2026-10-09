"""Shared file-scope resolution for contract validation and execution."""

from collections.abc import Iterable
from fnmatch import fnmatchcase


def scope_matches(item: str, files: Iterable[str]) -> list[str]:
    # A suffix after ':' identifies a symbol/line inside the selected file.
    pattern = item.split(":", 1)[0].strip()
    if not pattern:
        return []
    return [path for path in files if fnmatchcase(path, pattern)]


def resolve_scope(scope: Iterable[str], files: Iterable[str]) -> list[str]:
    files = list(files)
    return list(dict.fromkeys(path for item in scope for path in scope_matches(item, files)))
