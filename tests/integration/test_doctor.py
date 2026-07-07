"""`cartogate doctor` — the health check that makes a silent failure visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.doctor import run


def test_doctor_reports_healthy_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

    code = run(tmp_path)
    out = capsys.readouterr().out

    assert code == 0  # no hard failure
    assert "daemon not running" in out  # advisory, not a failure
    assert "gate answers (in-process)" in out  # the live probe succeeded
    assert "Cartogate is healthy" in out


def test_doctor_handles_a_dead_daemon_discovery_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stale discovery file pointing at a dead pid (a crashed daemon) reads as "not running" —
    # doctor warns and still proves the in-process gate works, rather than erroring out.
    import json

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    state = tmp_path / ".cartogate"
    state.mkdir()
    (state / "daemon.json").write_text(
        json.dumps(
            {"host": "127.0.0.1", "port": 1, "pid": 999_999, "token": "x", "repo": str(tmp_path)}
        ),
        encoding="utf-8",
    )

    code = run(tmp_path)
    out = capsys.readouterr().out
    # A dead pid reads as "not running" (psutil): doctor warns + still proves the in-process gate.
    assert "daemon not running" in out
    assert "gate answers (in-process)" in out
    assert code == 0
