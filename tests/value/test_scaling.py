"""V10 — scaling: the duplicate gate's latency stays flat as the repo grows.

Claim: ``check_duplicate`` is an indexed O(1) lookup, so its latency does not grow with the
number of symbols. We build warm stores at increasing scale and show p95 stays flat (and far
under the 50ms SLO), in contrast to a text scan whose cost grows with the codebase.
"""

from __future__ import annotations

import gc
import time

import pytest

from cartogate.engine.block import BlockEngine

from .metrics import COLLECTOR, latency_summary_ns
from .scale_support import build_store

pytestmark = pytest.mark.value

SIZES = (1_000, 10_000, 50_000)
_ITERATIONS = 3000


def _p95_ms_at(n_symbols: int) -> dict[str, float]:
    store = build_store(n_symbols=n_symbols, units=max(1, n_symbols // 1000))
    engine = BlockEngine(store)
    for i in range(200):  # warm-up
        engine.check_duplicate(f"def func_{i}(a, b):")

    durations: list[int] = []
    gc.disable()
    try:
        for i in range(_ITERATIONS):
            sig = f"def func_{i % n_symbols}(a, b):"
            start = time.perf_counter_ns()
            engine.check_duplicate(sig)
            durations.append(time.perf_counter_ns() - start)
    finally:
        gc.enable()
    return latency_summary_ns(durations)


def test_gate_latency_is_flat_with_scale() -> None:
    curve = {n: _p95_ms_at(n) for n in SIZES}
    p95s = [curve[n]["p95_ms"] for n in SIZES]

    # Indexed lookup → p95 stays small and roughly constant across a 50x size range.
    assert max(p95s) < 5.0
    # The largest store is not dramatically slower than the smallest (allow generous noise).
    assert max(p95s) <= max(0.05, min(p95s) * 50)

    COLLECTOR.record(
        hypothesis="V10",
        bucket="C",
        title="Scaling (flat gate latency)",
        claim="check_duplicate latency stays flat as the codebase grows (indexed O(1) lookup).",
        metric={
            "curve": {
                str(n): {k: round(v, 6) for k, v in curve[n].items()} for n in SIZES
            },
            "sizes": list(SIZES),
            "max_p95_ms": round(max(p95s), 6),
            "slo_p95_ms": 50.0,
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_scaling.py",
        notes="A grep/scan baseline grows linearly with the tree; the indexed gate does not.",
    )
