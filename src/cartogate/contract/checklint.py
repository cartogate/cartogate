"""Static lint for contract ``run:`` checks (spec §5).

Ported from scrimp ``checklint.py`` (field evidence: 20 ledger FAILs labeled check-defective;
the check author was the premium orchestrator). scrimp runs these warnings-only; contracts
upgrade them to refusal at declaration. A wrong-but-passing check (scrimp defect t2) is beyond
static lint — bounded by falsifiability tracking in stats, not closed (spec §8/§10).
"""
from __future__ import annotations

import re

from cartogate.contract.schema import Contract

_OUTPUT_TOKENS = ("echo", "print", "diff", "cat ", "assert", "FAIL", ">&2", "-v", "--verbose")
_SUBSHELL = re.compile(r"\$\([^)]*\)")


def _is_tautology(run: str) -> bool:
    low = run.strip().lower()
    if re.search(r"\bor\s+(true|1)\b", low) or re.search(r"\|\|\s*(true|exit 0)\b", low):
        return True
    if " or " in run or " || " in run:
        # X==a or X!=b with the same left operand passes ANY implementation.
        if re.search(r"(\w+(?:\([^)]*\))?)\s*==\s*(\S+)\s+or\s+\1\s*!=\s*", run):
            return True
        if re.search(r"test\s+(\S+)\s+-eq\s+\1", run):
            return True
    return False


def lint_check(run: str) -> list[str]:
    """Findings for one check command; empty list = clean."""
    findings: list[str] = []
    stripped = run.strip()
    if stripped in {"", "true", "exit 0", ":"} or stripped.endswith(("|| true", "|| exit 0")):
        findings.append("check cannot fail — it proves nothing about the change")
    if _is_tautology(run):
        findings.append("check is a tautology — it passes any implementation")
    visible = _SUBSHELL.sub("", run)
    has_output = any(tok in visible for tok in _OUTPUT_TOKENS)
    quietish = " -q" in run or "--quiet" in run or stripped.startswith("test ")
    if quietish and not has_output and "cannot fail" not in " ".join(findings):
        findings.append(
            "check is silent on failure — print WHY it fails (echo/diff/-v); "
            "the block message and the ledger depend on it"
        )
    return findings


def lint(contract: Contract) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for a whole contract. Errors refuse declaration (spec §5)."""
    errors: list[str] = []
    for i, check in enumerate(contract.checks):
        errors.extend(f"checks[{i}] ({check.run!r}): {f}" for f in lint_check(check.run))
    warnings: list[str] = []
    if not contract.checks and contract.attest:
        warnings.append(
            "attest-only contract — no executable checks; legal (pure design tasks exist) "
            "but every requirement rests on human judgment"
        )
    return errors, warnings
