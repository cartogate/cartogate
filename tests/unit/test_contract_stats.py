"""Contract observability — evidence mix + unfalsified checks (spec §8; scrimp t2 bound)."""
from __future__ import annotations

from pathlib import Path

from cartogate.audit import ledger
from cartogate.stats import contract_summary


def test_empty_repo_summary(tmp_path: Path) -> None:
    s = contract_summary(tmp_path)
    assert s == {"declared": 0, "checks": 0, "attests": 0, "unfalsified": []}


def test_mix_counts_from_declared_contracts(tmp_path: Path) -> None:
    ledger.append(tmp_path, entry_type="contract_declared", tree=None, evidence={
        "contract": {"task": "t", "checks": [{"run": "a -v"}, {"run": "b -v"}],
                     "attest": ["visual"]},
        "contract_hash": "h1", "lint_warnings": [],
    }, env={})
    s = contract_summary(tmp_path)
    assert s["declared"] == 1 and s["checks"] == 2 and s["attests"] == 1


def test_never_failed_check_is_flagged_unfalsified(tmp_path: Path) -> None:
    for _ in range(5):
        ledger.append(tmp_path, entry_type="contract_pass", tree="t", evidence={
            "contract_hash": "h", "checks": [{"run": "pytest  -v", "exit": 0,
                                              "output_hash": "x"}],
            "attest": {}, "diverged": False,
        }, env={})
    s = contract_summary(tmp_path)
    assert s["unfalsified"] == ["pytest -v"]  # whitespace-folded id


def test_a_single_failure_clears_the_flag(tmp_path: Path) -> None:
    for i in range(6):
        ledger.append(tmp_path, entry_type="contract_pass" if i else "contract_fail",
                      tree="t", evidence={
                          "contract_hash": "h",
                          "checks": [{"run": "pytest -v", "exit": 0 if i else 1,
                                      "output_hash": "x"}],
                          "attest": {}, "diverged": False,
                      }, env={})
    assert contract_summary(tmp_path)["unfalsified"] == []


def test_below_threshold_is_not_accused(tmp_path: Path) -> None:
    for _ in range(4):
        ledger.append(tmp_path, entry_type="contract_pass", tree="t", evidence={
            "contract_hash": "h", "checks": [{"run": "pytest -v", "exit": 0,
                                              "output_hash": "x"}],
            "attest": {}, "diverged": False,
        }, env={})
    assert contract_summary(tmp_path)["unfalsified"] == []


def test_malformed_ledger_entries_are_skipped_not_crashing(tmp_path: Path) -> None:
    """Review M3: a syntactically-valid but wrong-shape ledger entry (tampered/hand-edited)
    must be skipped by the reader, never crash `cartogate stats`."""
    ledger.append(tmp_path, entry_type="contract_declared", tree=None,
                  evidence={"contract": "not-a-dict"}, env={})
    ledger.append(tmp_path, entry_type="contract_pass", tree="t",
                  evidence={"checks": ["not-a-dict"]}, env={})
    s = contract_summary(tmp_path)
    assert s["declared"] == 1  # the entry is counted...
    assert s["checks"] == 0 and s["unfalsified"] == []  # ...but malformed evidence adds nothing


def test_sealed_runs_feed_falsifiability(tmp_path: Path) -> None:
    for _ in range(5):
        ledger.append(tmp_path, entry_type="sealed_pass", tree="t", evidence={
            "contract_hash": "h", "checks": [{"run": "held -v", "exit": 0,
                                              "output_hash": "x"}]}, env={})
    assert contract_summary(tmp_path)["unfalsified"] == ["held -v"]
