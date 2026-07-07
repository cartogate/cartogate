"""V3 — navigation accuracy: find_references recovers the true referencing set.

Claim: Cartogate's resolved graph answers "what depends on X" with higher precision than a
name-grep, because grep cannot tell a real reference from the same word in a comment or a
different same-named symbol. Measured against a hand-labeled fixture; the baseline is the
realistic ``grep \\bname\\b`` over the source tree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cartogate.mcp.tools import CartogateTools

from . import baselines, labels
from .metrics import COLLECTOR, classify

pytestmark = pytest.mark.value

FIXTURE = Path(__file__).parent / "fixtures" / "proj"


def _basenames(units: set[str]) -> set[str]:
    return {Path(u).name for u in units}


def test_find_references_beats_grep_precision(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj")
    refs = tools.find_references(labels.NAV_TARGET)["references"]
    cartogate_units = _basenames({r["unit"] for r in refs})
    grep_units = _basenames(baselines.units_referencing(FIXTURE, labels.NAV_BARE))

    gg = classify(cartogate_units, labels.NAV_TRUTH_UNITS)
    grep = classify(grep_units, labels.NAV_TRUTH_UNITS)

    # Cartogate is exact; grep over-matches the comment + the same-named billing.validate.
    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert gg.precision > grep.precision

    COLLECTOR.record(
        hypothesis="V3_fixture",
        bucket="A",
        title="Navigation accuracy (find_references vs grep) — illustrative fixture",
        claim="Cartogate recovers the true referencing files with higher precision than name-grep.",
        metric={
            "target": labels.NAV_TARGET,
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
            "cartogate_units": sorted(cartogate_units),
            "grep_units": sorted(grep_units),
            "truth_units": sorted(labels.NAV_TRUTH_UNITS),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_nav_accuracy.py",
        notes="Grep false-positives: helpers.py (comment) and billing.py (a different symbol).",
    )
