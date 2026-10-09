"""Fixed test executor.

Runs pytest with backend-defined arguments only, in an isolated execution
directory, under a hard timeout that terminates the whole process group and with
bounded output. The classification of the outcome (assertion failure vs
collection error vs zero tests vs all skipped vs missing dependency vs timeout
vs crash) is produced here and consumed by the check contract, so an exit code of
zero alone never means "passed" (§7.5.5).
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT_CHARS = 20000
MAX_OUTPUT_BYTES = 20000
PYTEST_ARGS = ["-q", "-s", "-p", "no:cacheprovider", "--tb=short", "-rN"]


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


_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|warning|warnings|deselected)"
)
_MISSING_DEP_RE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")
_IMPORT_ERR_RE = re.compile(r"(ImportError|ModuleNotFoundError):")


def run_pytest(workdir: Path, target: list[str], timeout_seconds: float,
               cancel_event: threading.Event | None = None) -> PytestOutcome:
    """Run a bounded subprocess and always reap its whole process group."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(workdir), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    cmd = [sys.executable, "-m", "pytest", *PYTEST_ARGS, *target]
    timed_out = False
    # Disable pytest's own file capture (-s). Drain pipes while the process is
    # alive, keeping only a bounded tail; noisy tests never grow a capture file.
    try:
        proc = subprocess.Popen(cmd, cwd=str(workdir), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except FileNotFoundError as exc:
        return PytestOutcome(exit_code=-2, missing_dependency="pytest",
                             summary_line=f"无法启动 pytest: {exc}", target=list(target))
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
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
                timed_out = True
                break
            drain(min(0.1, max(deadline - time.monotonic(), 0.001)))
    finally:
        # Reap descendants even if the session leader exited first.
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
    stdout = _tail(bytes(tails["stdout"]))
    stderr = _tail(bytes(tails["stderr"]))
    if timed_out:
        return PytestOutcome(exit_code=-1, timed_out=True,
                             summary_line=f"pytest 超时或已取消（期限 {timeout_seconds:.1f}s）",
                             stdout=stdout, stderr=stderr, target=list(target))
    counts: dict[str, int] = {}
    # Only the final pytest summary is authoritative; traceback text can contain counts.
    summary_line = _summary_line(stdout)
    for value, name in _COUNT_RE.findall(summary_line):
        key = name.rstrip("s") if name in {"errors", "warnings"} else name
        counts[key] = counts.get(key, 0) + int(value)
    counts["collected"] = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "skipped"))
    collection_error = "errors during collection" in stdout or "error during collection" in stdout
    missing = _MISSING_DEP_RE.search(stdout + stderr)
    missing_dependency = missing.group(1) if missing else None
    if not missing_dependency and collection_error and _IMPORT_ERR_RE.search(stdout + stderr):
        missing_dependency = "unknown"
    return PytestOutcome(exit_code=proc.returncode, counts=counts,
                         collection_error=collection_error, missing_dependency=missing_dependency,
                         summary_line=summary_line, stdout=stdout, stderr=stderr, target=list(target))


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
    run_dir = base_dir / f"run-{uuid.uuid4().hex[:8]}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
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
