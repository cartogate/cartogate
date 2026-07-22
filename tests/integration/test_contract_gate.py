"""Contract gate — the commit refuses "done" without the declared evidence (spec §6)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.conftest import git_cmd as _git
from tests.conftest import init_git_repo as _init

from cartogate.audit import ledger
from cartogate.precommit import main as precommit_main
from cartogate.task_cli import main as task_main

_PY = f'"{sys.executable}"'
# NOTE: the shared git helpers live in tests/conftest.py — do NOT define local _git/_init
# copies (the duplicate gate blocks the 19th copy; consolidated 2026-07-17).


def _declare(repo: Path, data: dict) -> None:  # type: ignore[type-arg]
    p = repo / "contract.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert task_main(["declare", str(p)]) == 0


def _stage_clean_file(repo: Path) -> None:
    (repo / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(repo, "add", "a.py")


def test_no_contract_means_no_behavior_change(tmp_path: Path) -> None:
    _init(tmp_path)
    _stage_clean_file(tmp_path)
    assert precommit_main([str(tmp_path)]) == 0  # exactly today's gate


def test_failing_check_blocks_with_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    code = "import sys; print('3 tests failed'); sys.exit(1)"
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "{code}"'}]})
    assert precommit_main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "CONTRACT BLOCKED" in err and "3 tests failed" in err
    assert "--abandon" in err  # the escape hatch is named, never hidden
    assert ledger.read(tmp_path)[-1]["type"] == "contract_fail"


def test_satisfied_contract_passes_and_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    assert precommit_main([str(tmp_path)]) == 0
    types = [e["type"] for e in ledger.read(tmp_path)]
    assert "contract_pass" in types and types.index("contract_pass") < types.index("commit_pass")


def test_missing_attestation_blocks_with_the_exact_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    _declare(tmp_path, {"task": "t", "attest": ["visual-signoff"]})
    assert precommit_main([str(tmp_path)]) == 1
    assert "cartogate task attest visual-signoff" in capsys.readouterr().err


def test_satisfied_attestation_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    _declare(tmp_path, {"task": "t", "attest": ["visual-signoff"]})
    assert task_main(["attest", "visual-signoff"]) == 0
    assert precommit_main([str(tmp_path)]) == 0


def test_corrupt_active_contract_blocks_not_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert precommit_main([str(tmp_path)]) == 1  # fail closed — never silently unenforced
    assert "unreadable" in capsys.readouterr().err


def test_duplicate_block_still_wins_before_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    assert precommit_main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "BLOCKED:" in err  # the duplicate gate fired; contract not the cause


def test_surrogate_check_output_still_ledgers_contract_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Medium (PR B): the OSError spawn path can carry surrogate-escaped bytes in
    CheckResult.output; evidence building must degrade (errors=replace), never crash past
    the ledger append — every decision path is ledgered."""
    from cartogate.contract import verify as cverify
    from cartogate.precommit import _enforce_contract

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    bad = cverify.ContractStatus(
        checks=(cverify.CheckResult("boom", None, "could not run: \udcff"),),
        attest={}, tree=None, diverged=False,
    )
    monkeypatch.setattr("cartogate.contract.verify.evaluate", lambda c, r: bad)
    assert _enforce_contract(tmp_path) == 1
    err = capsys.readouterr().err
    assert "CHECK FAILED" in err  # the real diagnosis, not the generic enforcement wrapper
    assert ledger.read(tmp_path)[-1]["type"] == "contract_fail"


def test_scope_advisory_failure_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Critical (PR C): an exception inside the scope advisory must never flip a
    passing contract into a block — an advisory never breaks the commit gate."""
    import fnmatch

    from cartogate.precommit import _enforce_contract

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                        "scope": {"files": ["*.py", "contract.json"]}})

    def _boom(*a: object, **k: object) -> bool:
        raise RuntimeError("advisory machinery exploded")

    monkeypatch.setattr(fnmatch, "fnmatch", _boom)
    monkeypatch.setattr(fnmatch, "fnmatchcase", _boom)
    assert _enforce_contract(tmp_path) == 0  # advisory failure swallowed; the contract PASSED
    assert ledger.read(tmp_path)[-1]["type"] == "contract_pass"


def test_tampered_state_content_blocks_with_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Critical (PR A): the gate anchors the worker-writable task.json to its
    hash-chained ledger declaration — hand-editing the contract's checks blocks with a
    state_divergence entry, never silently passes."""
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    _stage_clean_file(tmp_path)
    code = "import sys; sys.exit(1)"
    _declare(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "{code}"'}]})
    data = json.loads(state.task_path(tmp_path).read_text(encoding="utf-8"))
    data["contract"]["checks"] = [{"run": f'{_PY} -c "print(1)"'}]  # hand-'fixed' to pass
    state.task_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert precommit_main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "ledger declaration" in err
    assert ledger.read(tmp_path)[-1]["type"] == "state_divergence"
