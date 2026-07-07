"""V7 — test selection: pick the tests that exercise a change.

Claim: ``suggest_tests`` selects exactly the tests that exercise a changed symbol, so a
developer runs a fraction of the suite without losing any relevant coverage. We report
precision/recall against the hand-labeled exercising set, plus the suite-reduction ratio
versus the run-everything baseline.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cartogate.mcp.tools import CartogateTools

from . import labels
from .metrics import COLLECTOR, classify

pytestmark = pytest.mark.value


def test_suggest_tests_selects_relevant_subset(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj")
    report = tools.suggest_tests(symbols=[labels.TEST_TARGET])
    selected = {t["qualified_name"] for t in report["tests"]}

    gg = classify(selected, labels.TEST_TRUTH)
    reduction = 1.0 - (len(selected) / labels.TEST_SUITE_SIZE)

    # No relevant test missed (recall 1.0) and no irrelevant test selected (precision 1.0).
    assert gg.recall == 1.0
    assert gg.precision == 1.0
    assert reduction > 0.0

    COLLECTOR.record(
        hypothesis="V7_fixture",
        bucket="B",
        title="Test selection (suggest_tests) — illustrative fixture (direct unit tests)",
        claim="Selects the tests exercising a change — run fewer tests, lose no relevant coverage.",
        metric={
            "target": labels.TEST_TARGET,
            "cartogate": gg.to_dict(),
            "selected_tests": sorted(selected),
            "truth_tests": sorted(labels.TEST_TRUTH),
            "suite_size": labels.TEST_SUITE_SIZE,
            "selected_count": len(selected),
            "suite_reduction_ratio": round(reduction, 4),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_test_selection.py",
        notes="On this fixture, 1 of 3 tests is relevant — a 67% reduction with "
        "full relevant recall.",
    )
