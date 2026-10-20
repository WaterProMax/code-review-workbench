"""Bubblewrap must hide the application, the project .env, and the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.tools.sandbox import sandbox_argv
from app.services.tools.test_executor import run_pytest
from app.settings import PROJECT_ROOT


def test_probe_cannot_import_app_read_env_or_open_a_socket(tmp_path: Path) -> None:
    env_path = PROJECT_ROOT / ".env"
    probe = (
        "import socket\n"
        "from pathlib import Path\n"
        "def test_isolation():\n"
        "    try:\n"
        "        import app\n"
        "    except Exception:\n"
        "        blocked = True\n"
        "    else:\n"
        "        blocked = False\n"
        "    assert blocked\n"
        f"    env = Path({str(env_path)!r})\n"
        "    assert not env.exists()\n"
        "    try:\n"
        "        env.read_text(encoding='utf-8')\n"
        "    except Exception:\n"
        "        unreadable = True\n"
        "    else:\n"
        "        unreadable = False\n"
        "    assert unreadable\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
        "    except Exception:\n"
        "        offline = True\n"
        "    else:\n"
        "        offline = False\n"
        "    assert offline\n"
    )
    (tmp_path / "test_probe.py").write_text(probe, encoding="utf-8")
    outcome = run_pytest(tmp_path, ["test_probe.py"], 30)
    blob = outcome.stdout + outcome.stderr
    assert "sekrit" not in blob
    assert outcome.integrity_ok, blob
    assert outcome.passed == 1, blob
    assert outcome.failed == 0


def test_missing_bwrap_is_an_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.tools.sandbox.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="SANDBOX_UNAVAILABLE"):
        sandbox_argv(tmp_path, ["-c", "print(1)"])
