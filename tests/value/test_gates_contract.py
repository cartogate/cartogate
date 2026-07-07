"""V5 — contract gate: flag signature/visibility-narrowing breaks.

Claim: ``check_contract`` detects a breaking change to an exported symbol (a changed
parameter list or a narrowed visibility) and passes safe changes (annotation-only edits,
widened visibility). There is no standard automated baseline for this — a developer reviews
by eye — so we report Cartogate's precision/recall on labeled breaking-vs-safe changes and
note the baseline as "none (manual review)".
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cartogate.engine.block import BlockEngine
from cartogate.schema.enums import Visibility
from cartogate.store import InMemoryStore

from . import labels
from .metrics import COLLECTOR, classify

pytestmark = pytest.mark.value


def test_contract_gate_flags_breaking_changes(
    fixture_store: Callable[[str], InMemoryStore],
) -> None:
    engine = BlockEngine(fixture_store("proj"))

    truth = {c.label for c in labels.CONTRACT_CASES if c.is_breaking}
    blocked: set[str] = set()
    for case in labels.CONTRACT_CASES:
        vis = Visibility(case.new_visibility) if case.new_visibility else None
        result = engine.check_contract(
            labels.CONTRACT_TARGET, new_signature=case.new_signature, new_visibility=vis
        )
        if result.blocked:
            blocked.add(case.label)

    gg = classify(blocked, truth)
    assert gg.precision == 1.0
    assert gg.recall == 1.0

    COLLECTOR.record(
        hypothesis="V5_fixture",
        bucket="B",
        title="Contract-break detection (check_contract) — illustrative fixture",
        claim="Flags breaking signature/visibility changes to an exported symbol; "
        "passes safe ones.",
        metric={
            "target": labels.CONTRACT_TARGET,
            "cases": [
                {
                    "label": c.label,
                    "is_breaking": c.is_breaking,
                    "cartogate_blocked": c.label in blocked,
                    "note": c.note,
                }
                for c in labels.CONTRACT_CASES
            ],
            "cartogate": gg.to_dict(),
            "baseline": "none (no standard tool detects contract breaks automatically)",
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_gates_contract.py",
        notes="Baseline is manual code review; Cartogate makes the check deterministic.",
    )
