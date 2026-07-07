"""Go fixture — the four value props measured on a small Go module (illustrative fixture).

Mirrors the Python ``proj`` fixture appendix for Go: navigation precision (V3), the duplicate
gate (V4), test selection (V7), and doc-drift (V6), each against the realistic name-grep
baseline. The hand-labeled truth is read from the fixture source directly (not from Cartogate's
own output), so the precision/recall numbers are not circular.

This is the *runnable* (no-external-oracle) Go evidence. The headline real-repo Go study — an
independent oracle (``gopls`` references for V3, ``go test -coverprofile`` for V7) — is F-61's
deferred work, which needs the Go toolchain in the evaluation environment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from cartogate.mcp.tools import CartogateTools

from . import baselines
from .metrics import COLLECTOR, classify

pytestmark = pytest.mark.value

FIXTURE = Path(__file__).parent / "fixtures" / "proj_go"
GO = (".go",)

# --- V3: which files truly reference proj_go.auth.Validate -------------------------------- #
# auth.Authenticate calls it (auth.go); api.Check calls it via the import (api.go); and
# auth.TestValidate calls it (auth_test.go). billing.Validate is a DIFFERENT symbol — a grep hit.
NAV_TARGET = "proj_go.auth.Validate"
NAV_BARE = "Validate"
NAV_TRUTH_UNITS = {"auth.go", "api.go", "auth_test.go"}


@dataclass(frozen=True)
class DupCase:
    signature: str
    bare_name: str
    is_duplicate: bool
    note: str


DUPLICATE_CASES: tuple[DupCase, ...] = (
    DupCase("func Validate(name string) bool", "Validate", True, "exact existing auth.Validate"),
    DupCase(
        "func Validate(x int, y int) bool", "Validate", False,
        "name exists (auth + billing) but the 2-arg signature matches neither — not a duplicate",
    ),
    DupCase(
        "func Charge(amount int, currency string) bool", "Charge", True,
        "exact existing billing.Charge",
    ),
    DupCase("func BrandNew(x int) bool", "BrandNew", False, "genuinely new symbol"),
)

# --- V7: tests that exercise proj_go.auth.Validate ---------------------------------------- #
# TestValidate calls Validate; TestAuthenticate calls Authenticate (not Validate directly).
# (qnames follow Cartogate's Go convention: {repo_id}.{package}.{func}.)
TEST_TARGET = "proj_go.auth.Validate"
TEST_TRUTH = {"proj_go.auth.TestValidate"}

# --- V6: docs that explicitly document proj_go.auth.Authenticate -------------------------- #
# README documents it (a `Authenticate` code span); docs/guide.md only mentions it in prose.
DOC_TARGET = "proj_go.auth.Authenticate"
DOC_BARE = "Authenticate"
DOC_TRUTH = {"README.md"}


def _basenames(units: set[str]) -> set[str]:
    return {Path(u).name for u in units}


def test_find_references_beats_grep_precision_go(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj_go")
    refs = tools.find_references(NAV_TARGET)["references"]
    cartogate_units = _basenames({r["unit"] for r in refs})
    grep_units = _basenames(baselines.units_referencing(FIXTURE, NAV_BARE, GO))

    gg = classify(cartogate_units, NAV_TRUTH_UNITS)
    grep = classify(grep_units, NAV_TRUTH_UNITS)

    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert gg.precision > grep.precision  # grep also hits billing.go (a different Validate)

    COLLECTOR.record(
        hypothesis="V3_go_fixture",
        bucket="A",
        title="Navigation accuracy (find_references vs grep) — Go fixture",
        claim="On Go, Cartogate recovers the true referencing files more precisely than name-grep.",
        metric={
            "target": NAV_TARGET,
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
            "cartogate_units": sorted(cartogate_units),
            "grep_units": sorted(grep_units),
            "truth_units": sorted(NAV_TRUTH_UNITS),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_go_fixture.py",
        notes="Grep false-positive: billing.go (a different Validate symbol).",
    )


def test_duplicate_gate_beats_name_grep_go(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj_go")
    truth = {c.signature for c in DUPLICATE_CASES if c.is_duplicate}
    gg_blocked = {
        c.signature for c in DUPLICATE_CASES if tools.check_duplicate(c.signature, "go")["blocked"]
    }
    grep_blocked = {
        c.signature for c in DUPLICATE_CASES if baselines.defines_callable(FIXTURE, c.bare_name, GO)
    }

    gg = classify(gg_blocked, truth)
    grep = classify(grep_blocked, truth)

    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert grep.precision < gg.precision  # grep over-blocks the 2-arg Validate (name exists)

    COLLECTOR.record(
        hypothesis="V4_go_fixture",
        bucket="B",
        title="Duplicate detection (check_duplicate vs name-grep) — Go fixture",
        claim="On Go, catches a real top-level duplicate by signature without false-flagging a "
        "same-named function with a different parameter list.",
        metric={
            "cases": [
                {
                    "signature": c.signature,
                    "is_duplicate": c.is_duplicate,
                    "cartogate_blocked": c.signature in gg_blocked,
                    "grep_blocked": c.signature in grep_blocked,
                    "note": c.note,
                }
                for c in DUPLICATE_CASES
            ],
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_go_fixture.py",
        notes="Grep over-blocks `Validate(x,y)` — the name exists but no top-level callable has "
        "that signature.",
    )


def test_suggest_tests_selects_the_exercising_test_go(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj_go")
    selected = {t["qualified_name"] for t in tools.suggest_tests(symbols=[TEST_TARGET])["tests"]}
    gg = classify(selected, TEST_TRUTH)

    assert gg.precision == 1.0  # TestAuthenticate (calls Authenticate, not Validate) excluded
    assert gg.recall == 1.0

    COLLECTOR.record(
        hypothesis="V7_go_fixture",
        bucket="B",
        title="Test selection (suggest_tests) — Go fixture",
        claim="On Go, selects exactly the test that exercises a changed symbol.",
        metric={
            "target": TEST_TARGET,
            "cartogate": gg.to_dict(),
            "selected_tests": sorted(selected),
            "truth_tests": sorted(TEST_TRUTH),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_go_fixture.py",
        notes="TestAuthenticate exercises Authenticate, not Validate, so it is correctly excluded.",
    )


def test_doc_drift_beats_md_grep_go(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj_go")
    gg_docs = {Path(d["path"]).name for d in tools.doc_drift(symbols=[DOC_TARGET])["docs"]}
    grep_docs = {Path(p).name for p in baselines.docs_referencing(FIXTURE, DOC_BARE)}

    gg = classify(gg_docs, DOC_TRUTH)
    grep = classify(grep_docs, DOC_TRUTH)

    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert grep.precision < gg.precision  # grep also matches the prose mention in guide.md

    COLLECTOR.record(
        hypothesis="V6_go_fixture",
        bucket="B",
        title="Doc-drift detection (doc_drift vs md-grep) — Go fixture",
        claim="On Go, flags the doc that explicitly documents a symbol with higher precision than "
        "grepping markdown.",
        metric={
            "target": DOC_TARGET,
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
            "cartogate_docs": sorted(gg_docs),
            "grep_docs": sorted(grep_docs),
            "truth_docs": sorted(DOC_TRUTH),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_go_fixture.py",
        notes="Grep false-positive: guide.md (a prose mention, not a code reference).",
    )
