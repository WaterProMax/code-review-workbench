"""Static guard against patches that forge pytest verdicts.

``find_tampering`` inspects added lines. ``reject_patch_text`` does the same for
a unified diff and also rejects new (or edited) pytest config files.
"""

from __future__ import annotations

TAMPER_PATTERNS = ("os._exit", "_pytest", "pytest_runtest_", "sys.modules", "builtins.")
_FORBIDDEN_BASENAMES = {"conftest.py", "pytest.ini", "setup.cfg", "tox.ini"}


def forbidden_test_config_name(path: str) -> bool:
    name = path.replace("\\", "/").strip().rsplit("/", 1)[-1]
    if name in _FORBIDDEN_BASENAMES:
        return True
    return name.endswith(".ini")


def find_tampering(added_lines: list[str] | str) -> list[str]:
    """Return the subset of ``added_lines`` that tries to forge a verdict.

    A unified diff string is accepted as well and parsed by ``reject_patch_text``.
    """
    if isinstance(added_lines, str):
        return reject_patch_text(added_lines)
    hits: list[str] = []
    for line in added_lines:
        if any(pattern in line for pattern in TAMPER_PATTERNS) or forbidden_test_config_name(line.strip()):
            hits.append(line)
    return hits


def _strip_diff_path(value: str) -> str:
    token = value.split("\t", 1)[0].strip()
    if token.startswith("a/") or token.startswith("b/"):
        return token[2:]
    return token


def reject_patch_text(diff_text: str) -> list[str]:
    """Tamper hits in a unified diff: offending added lines and forbidden paths."""
    if not diff_text:
        return []
    hits: list[str] = []
    old = ""
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            old = line[4:]
            continue
        if line.startswith("+++ "):
            new_path = _strip_diff_path(line[4:])
            old_path = _strip_diff_path(old)
            if forbidden_test_config_name(new_path) or forbidden_test_config_name(old_path):
                hits.append(new_path or old_path)
            continue
        if line.startswith("+"):
            hits.extend(find_tampering([line[1:]]))
    unique: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit in seen:
            continue
        seen.add(hit)
        unique.append(hit)
    return unique
