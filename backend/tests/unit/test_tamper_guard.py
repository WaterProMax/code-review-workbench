"""Tamper patterns, regression-file atomicity, and sandbox settings."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.services.tools.checks import (
    RegressionSuiteMissing,
    load_regression_file,
    update_regression_file,
)
from app.services.tools.sandbox import assert_sandbox_config, docker_argv
from app.services.tools.tamper import find_tampering, reject_patch_text
from app.settings import PROJECT_ROOT, Settings


def test_find_tampering_flags_verdict_forgeries() -> None:
    assert find_tampering(["os._exit(0)"]) == ["os._exit(0)"]
    assert find_tampering(["import _pytest.terminal as t"])
    assert find_tampering(["def pytest_runtest_makereport(item, call):"])
    assert find_tampering(["sys.modules['pytest'] = None"])
    assert find_tampering(["builtins.len = len"])
    assert find_tampering(["conftest.py"])
    assert find_tampering(["nested/pytest.ini"])
    assert find_tampering(["setup.cfg"])
    assert find_tampering(["tox.ini"])
    assert find_tampering(["custom.ini"])


def test_find_tampering_ignores_a_normal_function() -> None:
    lines = ["def add(a, b):", "    return a + b"]
    assert find_tampering(lines) == []
    assert find_tampering(["os.exit", "sys.exit(0)", "builtins", "import pytest"]) == []


def test_reject_patch_text_flags_new_config_and_exit() -> None:
    forged = (
        "--- /dev/null\n"
        "+++ b/helpers.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def add(a, b):\n"
        "+    import os; os._exit(0)\n"
    )
    assert any("os._exit" in hit for hit in reject_patch_text(forged))
    conftest = (
        "--- /dev/null\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert any("conftest.py" in hit for hit in reject_patch_text(conftest))
    ini = "--- /dev/null\n+++ b/pkg/local.ini\n@@ -0,0 +1 @@\n+x = 1\n"
    assert any(hit.endswith("local.ini") for hit in reject_patch_text(ini))
    clean = (
        "--- /dev/null\n"
        "+++ b/helpers.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def add(a, b):\n"
        "+    return a + b\n"
    )
    assert reject_patch_text(clean) == []
    assert find_tampering(clean) == []


def test_regression_write_is_atomic_and_missing_file_is_not_refrozen(tmp_path: Path) -> None:
    path = tmp_path / "AVG.json"
    stored = update_regression_file(path, "AVG", {"a.py": "def test_a():\n    assert False\n"})
    assert "a.py" in stored
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{")
    assert not list(tmp_path.glob("*.tmp"))
    path.unlink()
    with pytest.raises(RegressionSuiteMissing, match="REGRESSION_SUITE_MISSING"):
        load_regression_file(path)
    with pytest.raises(RegressionSuiteMissing, match="REGRESSION_SUITE_MISSING"):
        update_regression_file(path, "AVG", {"b.py": "def test_b():\n    assert False\n"})
    assert not path.exists()


def test_regression_updates_are_serialized(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def add(name: str) -> None:
        try:
            barrier.wait(timeout=5)
            update_regression_file(path, "AVG", {name: f"def test_{name}():\n    assert True\n"})
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    loaded = load_regression_file(path)
    assert set(loaded) == {"one", "two"}


def test_api_key_is_hidden_and_env_file_is_absolute() -> None:
    settings = Settings(model_api_key="sekrit-value")
    assert "sekrit-value" not in repr(settings)
    env_file = Settings.model_config["env_file"]
    assert env_file == str(PROJECT_ROOT / ".env")
    assert ".." not in Path(env_file).parts
    bare = Settings(sandbox="none", model_api_key=None)
    assert bare.sandbox == "none"


def test_unsandboxed_and_missing_docker_are_refused(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HW2_ALLOW_UNSANDBOXED", raising=False)
    with pytest.raises(RuntimeError, match="SANDBOX_UNAVAILABLE"):
        assert_sandbox_config(Settings(sandbox="none", model_api_key=None))
    monkeypatch.setenv("HW2_ALLOW_UNSANDBOXED", "1")
    assert_sandbox_config(Settings(sandbox="none", model_api_key=None))

    def which(name: str) -> str | None:
        return None

    monkeypatch.setattr("app.services.tools.sandbox.shutil.which", which)
    with pytest.raises(RuntimeError, match="SANDBOX_UNAVAILABLE"):
        assert_sandbox_config(Settings(sandbox="docker", model_api_key=None))
    monkeypatch.setattr(
        "app.services.tools.sandbox.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    argv = docker_argv(tmp_path, ["-m", "pytest"])
    assert argv[0].endswith("docker")
    assert "--network" in argv and "none" in argv
    assert "--read-only" in argv
    assert "--pids-limit" in argv and "256" in argv
    assert "--memory" in argv and "1g" in argv
    assert "--cpus" in argv and "1" in argv
    assert f"{tmp_path.resolve()}:/work:rw" in argv
    assert "-w" in argv and "/work" in argv
