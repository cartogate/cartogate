"""Contract schema — parse/validate/hash. Refusal at declaration is the design (spec §5)."""
from __future__ import annotations

import pytest

from cartogate.contract.schema import Contract, ContractError, contract_hash, loads, parse


def _minimal() -> dict[str, object]:
    return {"task": "t", "checks": [{"run": "pytest -q tests/x.py && echo ok"}]}


def test_minimal_contract_parses() -> None:
    c = parse(_minimal())
    assert isinstance(c, Contract)
    assert c.task == "t"
    assert c.checks[0].run.startswith("pytest")
    assert c.checks[0].timeout == 300.0  # default
    assert c.attest == () and c.scope_files == () and c.scope_symbols == ()


def test_full_contract_parses() -> None:
    c = parse({
        "task": "wire gate", "checks": [{"run": "cmd a && echo done", "timeout": 60}],
        "attest": ["visual-signoff"],
        "scope": {"files": ["src/x/*.py"], "symbols": ["pkg.mod.fn"]},
    })
    assert c.attest == ("visual-signoff",)
    assert c.scope_files == ("src/x/*.py",) and c.scope_symbols == ("pkg.mod.fn",)
    assert c.checks[0].timeout == 60.0


def test_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(ContractError, match="cheks"):  # typo protection — silence would eat it
        parse({"task": "t", "cheks": [{"run": "x"}]})


def test_unknown_check_key_and_scope_key_are_refused() -> None:
    with pytest.raises(ContractError, match="cmd"):
        parse({"task": "t", "checks": [{"cmd": "x"}]})
    with pytest.raises(ContractError, match="paths"):
        parse({"task": "t", "checks": [{"run": "x"}], "scope": {"paths": []}})


def test_no_evidence_requirement_is_refused() -> None:
    # A contract with neither checks nor attestations enforces nothing — meaningless.
    with pytest.raises(ContractError, match="evidence"):
        parse({"task": "t"})


def test_bad_types_are_refused() -> None:
    for bad in (
        [],                                            # not a dict
        {"task": "", "checks": [{"run": "x"}]},        # empty task
        {"task": "t", "checks": [{"run": ""}]},        # empty run
        {"task": "t", "checks": [{"run": "x", "timeout": -1}]},  # bad timeout
        {"task": "t", "attest": ["a", "a"]},           # duplicate attest names
        {"task": "t", "attest": [""]},                 # empty attest name
    ):
        with pytest.raises(ContractError):
            parse(bad)


def test_loads_rejects_non_json() -> None:
    with pytest.raises(ContractError, match="JSON"):
        loads("not json {")


def test_hash_is_deterministic_and_key_order_free() -> None:
    a = contract_hash({"task": "t", "checks": [{"run": "x"}]})
    b = contract_hash({"checks": [{"run": "x"}], "task": "t"})
    assert a == b and len(a) == 128  # blake2b hex


def test_raw_survives_roundtrip() -> None:
    data = _minimal()
    c = parse(data)
    assert c.raw == data and contract_hash(c.raw) == contract_hash(data)


def test_pathological_timeouts_are_refused() -> None:
    """Review H1: json accepts NaN/Infinity literals and bool is an int subclass — all three
    slipped the sign-only check. NaN then CRASHES subprocess.run; Infinity silently unbounds
    the one safety limit around arbitrary shell execution."""
    for raw in (
        '{"task":"t","checks":[{"run":"x -v","timeout":NaN}]}',
        '{"task":"t","checks":[{"run":"x -v","timeout":Infinity}]}',
    ):
        with pytest.raises(ContractError, match="timeout"):
            loads(raw)
    with pytest.raises(ContractError, match="timeout"):
        parse({"task": "t", "checks": [{"run": "x -v", "timeout": True}]})
    with pytest.raises(ContractError, match="timeout"):
        parse({"task": "t", "checks": [{"run": "x -v", "timeout": 4000}]})  # above the cap


def test_whitespace_only_strings_are_refused() -> None:
    """Review L2: attest/scope entries get the same .strip() guard as the task label."""
    with pytest.raises(ContractError):
        parse({"task": "t", "attest": ["   "]})
    with pytest.raises(ContractError):
        parse({"task": "t", "checks": [{"run": "x -v"}], "scope": {"files": [" "]}})


def test_sealed_block_parses() -> None:
    c = parse({"task": "t", "checks": [{"run": "x -v"}],
               "sealed": {"hash": "ab" * 64, "count": 2}})
    assert c.sealed_hash == "ab" * 64 and c.sealed_count == 2
    assert parse(_minimal()).sealed_hash is None  # absent -> None, backward compatible


def test_sealed_block_is_validated() -> None:
    for bad in (
        {"hash": "xyz", "count": 1},              # not hex / wrong length
        {"hash": "ab" * 64, "count": 0},          # non-positive count
        {"hash": "ab" * 64, "count": 1, "x": 1},  # unknown subkey
        "not-a-dict",
    ):
        with pytest.raises(ContractError, match="sealed"):
            parse({"task": "t", "checks": [{"run": "x -v"}], "sealed": bad})


def test_stop_budget_defaults_to_three() -> None:
    c = parse(_minimal())
    assert c.stop_budget == 3


def test_stop_budget_parses() -> None:
    c = parse({"task": "t", "checks": [{"run": "x -v"}], "stop_budget": 5})
    assert c.stop_budget == 5


def test_stop_budget_is_validated() -> None:
    """stop_budget must be a positive int ≤ 100, matching timeout style validation."""
    for bad in (
        {"task": "t", "checks": [{"run": "x -v"}], "stop_budget": 0},      # zero
        {"task": "t", "checks": [{"run": "x -v"}], "stop_budget": "3"},     # string
        {"task": "t", "checks": [{"run": "x -v"}], "stop_budget": 101},     # over cap
        {"task": "t", "checks": [{"run": "x -v"}], "stop_budget": True},    # bool (is int subclass)
    ):
        with pytest.raises(ContractError, match="stop_budget"):
            parse(bad)
