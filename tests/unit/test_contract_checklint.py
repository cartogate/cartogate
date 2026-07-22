"""Check lint — scrimp field evidence (spec §2/§5): self-declared checks are routinely weak.

Heuristics ported from scrimp checklint.py; upgraded from warnings-only to REFUSAL at
declaration (blocking a malformed contract before work starts is never unfair).
"""
from __future__ import annotations

from cartogate.contract.checklint import lint, lint_check
from cartogate.contract.schema import parse


def test_cant_fail_checks_are_flagged() -> None:
    for run in ("true", "exit 0", "pytest -q || true", "pytest -q || exit 0", ":"):
        assert any("cannot fail" in f for f in lint_check(run)), run


def test_tautologies_are_flagged() -> None:
    for run in ("python -c 'assert x==1 or True'", "test 1 -eq 1 || true"):
        assert lint_check(run), run
    assert any("tautology" in f for f in lint_check('python -c "assert f(1)==2 or f(1)!=3"'))


def test_silent_quiet_checks_are_flagged() -> None:
    findings = lint_check("pytest -q tests/x.py")
    assert any("silent" in f for f in findings)
    # ...but a quiet flag WITH visible output is fine.
    assert not lint_check("pytest -q tests/x.py && echo PASSED")


def test_honest_checks_pass_clean() -> None:
    assert lint_check("python -m pytest tests/unit/test_x.py -v") == []
    assert lint_check("diff expected.txt actual.txt") == []


def test_lint_contract_maps_errors_and_attest_only_warning() -> None:
    errors, warnings = lint(parse({"task": "t", "checks": [{"run": "exit 0"}]}))
    assert errors and not warnings
    assert "checks[0]" in errors[0]  # findings name the offending check
    errors, warnings = lint(parse({"task": "t", "attest": ["visual"]}))
    assert not errors
    assert any("attest-only" in w for w in warnings)  # legal but visible (spec §5.4)
    errors, warnings = lint(parse({"task": "t", "checks": [{"run": "pytest -v tests/"}]}))
    assert (errors, warnings) == ([], [])
