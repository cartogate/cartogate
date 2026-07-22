"""Contract evidence evaluation — exit codes + tree-pinned attestations (spec §3/§6)."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.conftest import git_cmd as _git
from tests.conftest import init_git_repo as _init

from cartogate.audit import ledger
from cartogate.contract import verify
from cartogate.contract.schema import contract_hash, parse

_PY = f'"{sys.executable}"'


def test_exit0_check_passes_and_output_is_captured(tmp_path: Path) -> None:
    c = parse({"task": "t", "checks": [{"run": f'{_PY} -c "print(41+1)"'}]})
    results = verify.run_checks(c, tmp_path)
    assert results[0].exit_code == 0 and "42" in results[0].output


def test_nonzero_check_fails_with_diagnosis(tmp_path: Path) -> None:
    code = "import sys; print('why it broke'); sys.exit(3)"
    c = parse({"task": "t", "checks": [{"run": f'{_PY} -c "{code}"'}]})
    results = verify.run_checks(c, tmp_path)
    assert results[0].exit_code == 3 and "why it broke" in results[0].output


def test_timeout_is_a_failure_not_a_hang(tmp_path: Path) -> None:
    c = parse({"task": "t",
               "checks": [{"run": f'{_PY} -c "import time; time.sleep(30)"', "timeout": 1}]})
    results = verify.run_checks(c, tmp_path)
    assert results[0].exit_code is None and "timed out" in results[0].output


def test_attest_pins_to_the_exact_tree(tmp_path: Path) -> None:
    _init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    c = parse({"task": "t", "attest": ["visual"]})
    assert verify.attest_status(c, tmp_path) == {"visual": False}  # nothing recorded yet
    tree = verify.current_tree(tmp_path)
    assert tree is not None
    ledger.append(tmp_path, entry_type="attestation", tree=tree,
                  evidence={"name": "visual", "contract_hash": contract_hash(c.raw)}, env={})
    assert verify.attest_status(c, tmp_path) == {"visual": True}
    # ANY tree change invalidates the sign-off by construction (spec §6.2).
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    assert verify.attest_status(c, tmp_path) == {"visual": False}


def test_attest_for_a_different_contract_does_not_count(tmp_path: Path) -> None:
    _init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    c = parse({"task": "t", "attest": ["visual"]})
    tree = verify.current_tree(tmp_path)
    ledger.append(tmp_path, entry_type="attestation", tree=tree,
                  evidence={"name": "visual", "contract_hash": "someone-else"}, env={})
    assert verify.attest_status(c, tmp_path) == {"visual": False}


def test_evaluate_composes_and_flags_divergence(tmp_path: Path) -> None:
    _init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    c = parse({"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    status = verify.evaluate(c, tmp_path)
    assert status.ok and status.tree and status.diverged is False
    (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8")  # tracked, UNSTAGED edit
    assert verify.evaluate(c, tmp_path).diverged is True  # working dir != index (spec §6.1)


def test_non_git_dir_fails_closed_never_vacuous(tmp_path: Path) -> None:
    """Review L1: when git can't produce a tree, attestations are UNSATISFIED (fail-closed) —
    a refactor that returns before the None-check would flip this to vacuous-pass."""
    c = parse({"task": "t", "attest": ["visual"]})
    assert verify.attest_status(c, tmp_path) == {"visual": False}
    assert verify.evaluate(c, tmp_path).ok is False
