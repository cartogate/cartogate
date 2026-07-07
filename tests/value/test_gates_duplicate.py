"""V4 — duplicate gate: catch real duplicates without false-flagging.

Claim: ``check_duplicate`` is signature-shaped and scoped to top-level callables, so it
catches a re-declared function even when annotations/return-types/defaults differ (a textual
compare would miss it), while NOT flagging a same-named method or a same-named function with
a different parameter list (the dogfood false-positive classes). The baseline is the naive
``grep "def name("``.
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


def test_duplicate_gate_beats_name_grep(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj")

    truth = {c.signature for c in labels.DUPLICATE_CASES if c.is_duplicate}
    gg_blocked = {
        c.signature for c in labels.DUPLICATE_CASES if tools.check_duplicate(c.signature)["blocked"]
    }
    grep_blocked = {
        c.signature
        for c in labels.DUPLICATE_CASES
        if baselines.defines_callable(FIXTURE, c.bare_name)
    }

    gg = classify(gg_blocked, truth)
    grep = classify(grep_blocked, truth)

    # Cartogate matches the human labels exactly; name-grep over-blocks charge() and close().
    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert grep.precision < gg.precision

    COLLECTOR.record(
        hypothesis="V4_fixture",
        bucket="B",
        title="Duplicate detection (check_duplicate vs name-grep) — illustrative fixture",
        claim="Catches real duplicates (incl. annotation-only differences) without false-flagging "
        "same-named methods or different-signature functions.",
        metric={
            "cases": [
                {
                    "signature": c.signature,
                    "is_duplicate": c.is_duplicate,
                    "cartogate_blocked": c.signature in gg_blocked,
                    "grep_blocked": c.signature in grep_blocked,
                    "note": c.note,
                }
                for c in labels.DUPLICATE_CASES
            ],
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_gates_duplicate.py",
        notes="Grep false-positives: charge (different params) and close (a method).",
    )
