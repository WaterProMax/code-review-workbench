"""Process isolation for untrusted pytest runs.

The default is bubblewrap. A missing sandbox binary is an error: the executor
never falls back to an unsandboxed interpreter. ``sandbox="none"`` is an explicit
opt-in for local debugging and requires ``HW2_ALLOW_UNSANDBOXED=1``.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

from app.settings import Settings


def _site_packages() -> str:
    return sysconfig.get_paths()["purelib"]


def assert_sandbox_config(settings: Settings) -> None:
    """Refuse to run tests when the selected sandbox cannot actually isolate them.

    Importing the application must stay cheap: callers invoke this at
    ``run_pytest`` entry, not at import time.
    """
    mode = settings.sandbox
    if mode == "none":
        if os.environ.get("HW2_ALLOW_UNSANDBOXED") != "1":
            raise RuntimeError("SANDBOX_UNAVAILABLE")
        return
    if mode == "bwrap":
        if shutil.which("bwrap") is None:
            raise RuntimeError("SANDBOX_UNAVAILABLE")
        return
    if mode == "docker":
        if shutil.which("docker") is None:
            raise RuntimeError("SANDBOX_UNAVAILABLE")
        return
    raise RuntimeError("SANDBOX_UNAVAILABLE")


def sandbox_argv(
    run_dir: Path,
    inner: list[str],
    report_dir: Path | None = None,
    config_dir: Path | None = None,
) -> list[str]:
    """Build a ``bwrap`` command that runs ``inner`` under ``python3``.

    The project tree, ``.env`` and ``data/`` are not mounted. Site-packages are
    exposed only at ``/sp``, so an editable ``.pth`` that points at ``backend/``
    cannot make ``import app`` succeed.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("SANDBOX_UNAVAILABLE")
    base = sys.base_prefix
    site = _site_packages()
    work = Path(run_dir).resolve()
    argv: list[str] = [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
    ]
    # uv's base interpreter often *is* /usr. Binding it twice makes bwrap fail.
    if Path(base).resolve() != Path("/usr"):
        argv.extend(["--ro-bind", base, base])
    argv.extend(
        [
            "--ro-bind",
            site,
            "/sp",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(work),
            "/work",
            "--chdir",
            "/work",
        ]
    )
    if report_dir is not None:
        argv.extend(["--bind", str(Path(report_dir).resolve()), "/report"])
    if config_dir is not None:
        argv.extend(["--ro-bind", str(Path(config_dir).resolve()), "/sandbox-config"])
    argv.extend(
        [
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "--setenv",
            "HOME",
            "/work",
            "--setenv",
            "PYTHONPATH",
            "/work:/sp",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            f"{base}/bin/python3",
            *inner,
        ]
    )
    return argv


def docker_argv(
    run_dir: Path,
    inner: list[str],
    report_dir: Path | None = None,
    config_dir: Path | None = None,
) -> list[str]:
    """Docker fallback. The image is not required for the default bwrap tests."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("SANDBOX_UNAVAILABLE")
    work = Path(run_dir).resolve()
    argv: list[str] = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "-v",
        f"{work}:/work:rw",
        "-w",
        "/work",
    ]
    if report_dir is not None:
        argv.extend(["-v", f"{Path(report_dir).resolve()}:/report:rw"])
    if config_dir is not None:
        argv.extend(["-v", f"{Path(config_dir).resolve()}:/sandbox-config:ro"])
    argv.extend(["sandbox-py:3.12", "python3", *inner])
    return argv


def execution_argv(
    settings: Settings,
    run_dir: Path,
    inner: list[str],
    report_dir: Path | None = None,
    config_dir: Path | None = None,
) -> list[str]:
    mode = settings.sandbox
    if mode == "bwrap":
        return sandbox_argv(run_dir, inner, report_dir=report_dir, config_dir=config_dir)
    if mode == "docker":
        return docker_argv(run_dir, inner, report_dir=report_dir, config_dir=config_dir)
    if mode == "none":
        return [sys.executable, *inner]
    raise RuntimeError("SANDBOX_UNAVAILABLE")
