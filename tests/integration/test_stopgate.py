"""Stop-gate: bounded refusal for locked, unsatisfied contracts (spec §4)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from tests.conftest import init_git_repo as _init
from tests.conftest import write_contract as _write_contract

from cartogate.audit import ledger
from cartogate.stopgate import main as stopgate_main
from cartogate.task_cli import main as task_main

_PY = f'"{sys.executable}"'


def test_stopgate_allows_no_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No active contract → allow (exit 0)."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 0


def test_stopgate_allows_unlocked_unsatisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlocked contract (even if unsatisfied) → allow (exit 0)."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(
        tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "import sys; sys.exit(1)"'}]}
    )
    task_main(["declare", str(p)])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 0


def test_stopgate_allows_locked_satisfied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Locked satisfied contract → allow (exit 0)."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(
        tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]}
    )
    task_main(["declare", str(p), "--lock"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 0


def test_stopgate_refuses_locked_unsatisfied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Locked unsatisfied → refuse with exit 2, stderr reason, ledger stop_refused."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(
        tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "import sys; sys.exit(1)"'}]}
    )
    task_main(["declare", str(p), "--lock"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 2
    err = capsys.readouterr().err
    assert "--abandon" in err  # remedy named
    assert "(refusal 1/" in err  # counter
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "stop_refused"
    assert entry["evidence"]["refusal"] == 1
    assert entry["evidence"]["budget"] == 3  # default


def test_stopgate_bounded_refusal_until_budget_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bounded refusals: refuse thrice (budget=2), then allow with unsatisfied_stop."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(
        tmp_path,
        {
            "task": "t",
            "checks": [{"run": f'{_PY} -c "import sys; sys.exit(1)"'}],
            "stop_budget": 2,
        },
    )
    task_main(["declare", str(p), "--lock"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    # First refusal
    assert stopgate_main() == 2
    capsys.readouterr()
    # Second refusal
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 2
    capsys.readouterr()
    # Third call: budget exhausted
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 0
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "unsatisfied_stop"
    assert entry["evidence"]["refusals"] == 2


def test_stopgate_allows_corrupt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt task.json → allow (fail-open pinned)."""
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert stopgate_main() == 0


def test_stopgate_tolerates_garbage_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage stdin → no raise; behaves by cwd repo."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("not valid json"))
    # Should not raise, should return 0 (no contract)
    assert stopgate_main() == 0


def test_stopgate_reads_payload_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload with cwd key doesn't crash (even if it's not resolvable)."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    payload = {"cwd": "/nonexistent/path"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    # Should not crash; returns 0 because /nonexistent is not a repo
    assert stopgate_main() == 0
