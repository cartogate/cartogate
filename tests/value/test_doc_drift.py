"""V6 — doc-drift: find docs that document a changed symbol, conservatively.

Claim: ``doc_drift`` flags docs that explicitly reference a symbol (a backtick code span or
a file link) with high precision, where a name-grep over ``*.md`` over-matches incidental
prose. Measured against a hand-labeled fixture.
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


def test_doc_drift_beats_grep_precision(
    fixture_tools: Callable[[str], CartogateTools],
) -> None:
    tools = fixture_tools("proj")
    docs = tools.doc_drift(symbols=[labels.DOC_TARGET])["docs"]
    cartogate_docs = {Path(d["unit"]).name for d in docs}
    grep_docs = {Path(u).name for u in baselines.docs_referencing(FIXTURE, labels.DOC_BARE)}

    gg = classify(cartogate_docs, labels.DOC_TRUTH_UNITS)
    grep = classify(grep_docs, labels.DOC_TRUTH_UNITS)

    # Cartogate flags only the README's explicit `authenticate` span; grep also matches the
    # English word "authenticate" in api.md and security.md.
    assert gg.precision == 1.0
    assert gg.recall == 1.0
    assert gg.precision > grep.precision

    COLLECTOR.record(
        hypothesis="V6_fixture",
        bucket="B",
        title="Doc-drift detection (doc_drift vs md-grep) — illustrative fixture",
        claim="Flags docs explicitly documenting a symbol with higher precision "
        "than grepping markdown.",
        metric={
            "target": labels.DOC_TARGET,
            "cartogate": gg.to_dict(),
            "grep_baseline": grep.to_dict(),
            "cartogate_docs": sorted(cartogate_docs),
            "grep_docs": sorted(grep_docs),
            "truth_docs": sorted(labels.DOC_TRUTH_UNITS),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_doc_drift.py",
        notes="Conservative matching (exact qname or unique bare name) is what wins precision; "
        "ambiguous bare names are skipped (a known recall limitation).",
    )
