"""Fixed test executor.

Runs pytest with backend-defined arguments only, in an isolated execution
directory, under a hard timeout that terminates the whole process group and with
bounded output. Pass/fail comes from a junit report written outside the
untrusted tree and checked against a separate ``--collect-only`` run. An exit
code of zero, or a summary line printed by the code under test, never means
"passed" on its own.
"""

from __future__ import annotations

import os
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from app.services.tools.sandbox import assert_sandbox_config, execution_argv
from app.settings import get_settings

MAX_OUTPUT_CHARS = 20000
MAX_OUTPUT_BYTES = 20000
PYTEST_ARGS = [
    "-q",
    "-s",
    "-p",
    "no:cacheprovider",
    "--tb=short",
    "-rN",
    "--noconftest",
]
# Collection is our own step. A one-second test timeout must still reach the
# test body (descendant-reaping tests), so collection gets a small floor.
_COLLECT_TIMEOUT_FLOOR = 15.0


@dataclass
class PytestOutcome:
    exit_code: int
    timed_out: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    collection_error: bool = False
    missing_dependency: str | None = None
    summary_line: str = ""
    stdout: str = ""
    stderr: str = ""
    target: list[str] = field(default_factory=list)
    # False when junit is missing or does not name the same tests collect-only saw.
    integrity_ok: bool = True
    failed_node_ids: list[str] = field(default_factory=list)
    collected_node_ids: list[str] = field(default_factory=list)

    @property
    def collected(self) -> int:
        return int(self.counts.get("collected", 0))

    @property
    def passed(self) -> int:
        return int(self.counts.get("passed", 0))

    @property
    def failed(self) -> int:
        return int(self.counts.get("failed", 0)) + int(self.counts.get("error", 0))

    @property
    def skipped(self) -> int:
        return int(self.counts.get("skipped", 0))

    def zero_collected(self) -> bool:
        return self.collected == 0 and not self.collection_error


_MISSING_DEP_RE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")
_IMPORT_ERR_RE = re.compile(r"(ImportError|ModuleNotFoundError):")


def sandbox_environ(workdir: Path) -> dict[str, str]:
    """Environment for a generated-test process.

    Only a small allow-list is copied. ``HW2_*`` settings (API keys, data paths)
    are never inherited, and ``PYTHONPATH`` is the execution directory alone.
    Bubblewrap ignores this and applies ``--clearenv`` itself.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "COMSPEC"):
        value = os.environ.get(key)
        if value and not key.startswith("HW2_"):
            env[key] = value
    scratch = workdir / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(workdir),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "PYTHONPATH": str(workdir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        }
    )
    return env


def _cap_rlimit(resource_id: int, soft: int, hard: int) -> None:
    """Lower a limit without raising one the operating system already set."""
    try:
        current_soft, current_hard = resource.getrlimit(resource_id)
    except (ValueError, OSError):
        return

    def clamp(wanted: int, current: int) -> int:
        if current == resource.RLIM_INFINITY:
            return wanted
        return min(wanted, current)

    try:
        resource.setrlimit(resource_id, (clamp(soft, current_soft), clamp(hard, current_hard)))
    except (ValueError, OSError):
        return


def apply_sandbox_rlimits() -> None:
    """CPU, file-size and address-space caps for the test child.

    Process count is not limited here. ``RLIMIT_NPROC`` is per user, so a cap of
    256 makes pytest fail to fork on a busy machine. The sandbox owns that
    limit (bubblewrap's pid namespace, or Docker ``--pids-limit``).
    Address-space limits are applied on Linux only. On macOS ``RLIMIT_AS`` counts
    the virtual mapping of the Python runtime and makes pytest fail to start.
    """
    if hasattr(resource, "RLIMIT_CPU"):
        _cap_rlimit(resource.RLIMIT_CPU, 60, 60)
    if hasattr(resource, "RLIMIT_FSIZE"):
        _cap_rlimit(resource.RLIMIT_FSIZE, 16 * 1024 * 1024, 16 * 1024 * 1024)
    if sys.platform.startswith("linux") and hasattr(resource, "RLIMIT_AS"):
        two_gb = 2 * 1024 * 1024 * 1024
        _cap_rlimit(resource.RLIMIT_AS, two_gb, two_gb)


def _sandbox_paths(mode: str, workdir: Path, report_dir: Path, config_dir: Path) -> tuple[str, str, str]:
    if mode in {"bwrap", "docker"}:
        return "/work", "/sandbox-config/pytest.ini", "/report/junit.xml"
    return str(workdir), str(config_dir / "pytest.ini"), str(report_dir / "junit.xml")


def _pytest_inner(
    mode: str,
    workdir: Path,
    report_dir: Path,
    config_dir: Path,
    target: list[str],
    *,
    collect: bool,
) -> list[str]:
    rootdir, ini, junit = _sandbox_paths(mode, workdir, report_dir, config_dir)
    if collect:
        return [
            "-m",
            "pytest",
            "--noconftest",
            "-c",
            ini,
            "--rootdir",
            rootdir,
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            *target,
        ]
    return [
        "-m",
        "pytest",
        *PYTEST_ARGS,
        "-c",
        ini,
        "--rootdir",
        rootdir,
        f"--junitxml={junit}",
        *target,
    ]


@dataclass
class _CommandResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    missing_pytest: str | None = None


def _execute(cmd: list[str], workdir: Path, timeout_seconds: float,
             cancel_event: threading.Event | None) -> _CommandResult:
    env = sandbox_environ(workdir)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=apply_sandbox_rlimits,
        )
    except FileNotFoundError as exc:
        return _CommandResult(
            exit_code=-2,
            timed_out=False,
            stdout="",
            stderr="",
            missing_pytest=f"无法启动 pytest: {exc}",
        )
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    def drain(timeout: float) -> None:
        for key, _ in selector.select(timeout):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            tail = tails[key.data]
            tail.extend(chunk)
            if len(tail) > MAX_OUTPUT_BYTES:
                del tail[:-MAX_OUTPUT_BYTES]

    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    timed_out = False
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
                timed_out = True
                break
            drain(min(0.1, max(deadline - time.monotonic(), 0.001)))
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        try:
            drain_deadline = time.monotonic() + 1.0
            while selector.get_map() and time.monotonic() < drain_deadline:
                drain(0.1)
        finally:
            selector.close()
            proc.stdout.close()
            proc.stderr.close()
    return _CommandResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        timed_out=timed_out,
        stdout=_tail(bytes(tails["stdout"])),
        stderr=_tail(bytes(tails["stderr"])),
    )


def run_pytest(workdir: Path, target: list[str], timeout_seconds: float,
               cancel_event: threading.Event | None = None) -> PytestOutcome:
    """Run a bounded subprocess and always reap its whole process group."""
    settings = get_settings()
    assert_sandbox_config(settings)
    report_dir = Path(tempfile.mkdtemp(prefix="crw-junit-"))
    config_dir = Path(tempfile.mkdtemp(prefix="crw-pycfg-"))
    try:
        (config_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        mode = settings.sandbox
        collect_cmd = execution_argv(
            settings,
            workdir,
            _pytest_inner(mode, workdir, report_dir, config_dir, target, collect=True),
            report_dir=report_dir,
            config_dir=config_dir,
        )
        collected = _execute(collect_cmd, workdir, max(timeout_seconds, _COLLECT_TIMEOUT_FLOOR), cancel_event)
        if collected.missing_pytest:
            return PytestOutcome(
                exit_code=-2,
                missing_dependency="pytest",
                summary_line=collected.missing_pytest,
                target=list(target),
                integrity_ok=False,
            )
        if collected.timed_out:
            return PytestOutcome(
                exit_code=-1,
                timed_out=True,
                summary_line=f"pytest 超时或已取消（期限 {timeout_seconds:.1f}s）",
                stdout=collected.stdout,
                stderr=collected.stderr,
                target=list(target),
                integrity_ok=False,
            )
        run_cmd = execution_argv(
            settings,
            workdir,
            _pytest_inner(mode, workdir, report_dir, config_dir, target, collect=False),
            report_dir=report_dir,
            config_dir=config_dir,
        )
        ran = _execute(run_cmd, workdir, timeout_seconds, cancel_event)
        if ran.missing_pytest:
            return PytestOutcome(
                exit_code=-2,
                missing_dependency="pytest",
                summary_line=ran.missing_pytest,
                stdout=collected.stdout,
                stderr=collected.stderr,
                target=list(target),
                integrity_ok=False,
            )
        if ran.timed_out:
            return PytestOutcome(
                exit_code=-1,
                timed_out=True,
                summary_line=f"pytest 超时或已取消（期限 {timeout_seconds:.1f}s）",
                stdout=ran.stdout,
                stderr=ran.stderr,
                target=list(target),
                integrity_ok=False,
            )
        return _verdict(collected, ran, report_dir / "junit.xml", target)
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)
        shutil.rmtree(config_dir, ignore_errors=True)


def _junit_key(nodeid: str) -> tuple[str, str]:
    """Match pytest's ``mangle_test_address`` so junit names compare to node ids."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    if not names:
        return "", nodeid
    names[0] = names[0].replace("/", ".").replace("\\", ".")
    names[0] = re.sub(r"\.py$", "", names[0])
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _parse_collected(stdout: str) -> tuple[list[str], bool]:
    nodes: list[str] = []
    error = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("ERROR"):
            error = True
            continue
        if "::" in line and not line.startswith("="):
            nodes.append(line)
    if re.search(r"error during collection|errors during collection", stdout, re.IGNORECASE):
        error = True
    return nodes, error


def _parse_junit(path: Path) -> list[tuple[str, str, str]] | None:
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    cases: list[tuple[str, str, str]] = []
    for node in root.iter("testcase"):
        classname = node.attrib.get("classname", "")
        name = node.attrib.get("name", "")
        tags = {child.tag for child in list(node)}
        if "error" in tags:
            status = "error"
        elif "failure" in tags:
            status = "failed"
        elif "skipped" in tags:
            status = "skipped"
        else:
            status = "passed"
        cases.append((classname, name, status))
    return cases


def _verdict(
    collected: _CommandResult,
    ran: _CommandResult,
    junit_path: Path,
    target: list[str],
) -> PytestOutcome:
    node_ids, collect_error = _parse_collected(collected.stdout)
    if collected.exit_code not in (0, 1, 5) and not node_ids:
        collect_error = True
    cases = _parse_junit(junit_path)
    blob = "\n".join((collected.stdout, collected.stderr, ran.stdout, ran.stderr))
    missing = _MISSING_DEP_RE.search(blob)
    missing_dependency = missing.group(1) if missing and collect_error else None
    if collect_error and not missing_dependency and _IMPORT_ERR_RE.search(blob):
        missing_dependency = "unknown"

    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "collected": 0}
    failed_node_ids: list[str] = []
    integrity_ok = False
    if cases is not None:
        by_key: dict[tuple[str, str], str] = {}
        for node in node_ids:
            by_key.setdefault(_junit_key(node), node)
        junit_keys = [(classname, name) for classname, name, _status in cases]
        collected_keys = [_junit_key(node) for node in node_ids]
        integrity_ok = sorted(junit_keys) == sorted(collected_keys) and len(junit_keys) == len(collected_keys)
        for classname, name, status in cases:
            counts[status] = counts.get(status, 0) + 1
            if status in {"failed", "error"}:
                failed_node_ids.append(by_key.get((classname, name), f"{classname}::{name}"))
        counts["collected"] = len(cases)
    if cases is None or not integrity_ok:
        integrity_ok = False
    summary_line = _summary_line(ran.stdout)
    return PytestOutcome(
        exit_code=ran.exit_code,
        counts=counts,
        collection_error=collect_error,
        missing_dependency=missing_dependency,
        summary_line=summary_line,
        stdout=ran.stdout,
        stderr=ran.stderr,
        target=list(target),
        integrity_ok=integrity_ok and cases is not None,
        failed_node_ids=failed_node_ids,
        collected_node_ids=list(node_ids),
    )


def _tail(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-MAX_OUTPUT_CHARS:]


def _summary_line(stdout: str) -> str:
    lines = [line.strip() for line in stdout.strip().split("\n") if line.strip()]
    for line in reversed(lines):
        if re.search(r"(passed|failed|error|no tests ran|skipped)", line, re.IGNORECASE):
            return line[:300]
    return lines[-1][:300] if lines else ""


def set_up_execution_dir(
    *, base_dir: Path, snapshot_files: dict[str, bytes], test_files: dict[str, str]
) -> tuple[Path, dict[str, str]]:
    """Copy a snapshot plus generated tests into an isolated run directory.

    Returns the directory and the test-file map (relative path -> content) so the
    caller can hash exactly what was executed. The verified snapshot itself is
    never written to.
    """
    # ``base_dir`` stays in the signature for callers. The process itself runs
    # under the system temp directory so a relative path cannot reach the
    # business database or the project ``.env``.
    del base_dir
    run_dir = Path(tempfile.mkdtemp(prefix="crw-pytest-"))
    for rel, blob in snapshot_files.items():
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    written: dict[str, str] = {}
    for rel, content in test_files.items():
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written[rel] = content
    return run_dir, written


def cleanup_execution_dir(run_dir: Path) -> None:
    shutil.rmtree(run_dir, ignore_errors=True)
