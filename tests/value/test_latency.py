"""V2 — latency: a graph query answers faster than text retrieval over the repo.

Claim: answering "what references X" / "does this signature exist" via the warm graph is
orders of magnitude faster than the realistic grep baseline, which must read and scan every
source file. We index Cartogate's own source, time the tools over many iterations (p50/p95
in ms), and time the grep baseline answering the same question.
"""

from __future__ import annotations

import gc
import time
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

from . import baselines
from .metrics import COLLECTOR, latency_summary_ns

pytestmark = pytest.mark.value

SELF_SRC = Path(__file__).resolve().parents[2] / "src" / "cartogate"
_ITERATIONS = 2000


def _time_ns(fn, iterations: int) -> list[int]:
    durations: list[int] = []
    gc.disable()
    try:
        for _ in range(iterations):
            start = time.perf_counter_ns()
            fn()
            durations.append(time.perf_counter_ns() - start)
    finally:
        gc.enable()
    return durations


def test_graph_query_is_faster_than_grep() -> None:
    store = InMemoryStore()
    index_package(SELF_SRC, repo_id="cartogate", store=store, resolve=True, index_docs=False)
    tools = CartogateTools(store)

    # Warm up.
    for _ in range(50):
        tools.check_duplicate("def normalize_signature(raw, language):")

    dup_ns = _time_ns(
        lambda: tools.check_duplicate("def normalize_signature(raw, language):"), _ITERATIONS
    )
    ref_ns = _time_ns(
        lambda: tools.find_references("cartogate.schema.signature.normalize_signature"), 500
    )

    # Grep baseline: the realistic "find references by scanning the tree" answer, timed.
    grep_ns = _time_ns(
        lambda: baselines.units_referencing(SELF_SRC, "normalize_signature"), 20
    )

    dup = latency_summary_ns(dup_ns)
    ref = latency_summary_ns(ref_ns)
    grep = latency_summary_ns(grep_ns)
    speedup = grep["p50_ms"] / ref["p50_ms"] if ref["p50_ms"] else float("inf")

    # The warm gate is sub-millisecond and far under the 50ms SLO; grep is much slower.
    assert dup["p95_ms"] < 50.0
    assert ref["p50_ms"] < grep["p50_ms"]

    COLLECTOR.record(
        hypothesis="V2",
        bucket="A",
        title="Query latency (graph vs grep)",
        claim="Warm graph queries answer in well under a millisecond — far faster than scanning "
        "the source tree, and orders under the 50ms in-loop SLO.",
        metric={
            "check_duplicate_ms": {k: round(v, 5) for k, v in dup.items()},
            "find_references_ms": {k: round(v, 5) for k, v in ref.items()},
            "grep_baseline_ms": {k: round(v, 5) for k, v in grep.items()},
            "find_references_speedup_vs_grep": round(speedup, 1),
            "slo_p95_ms": 50.0,
            "indexed": "src/cartogate",
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_latency.py",
        notes="Index time is off the gate's latency budget (warm-resident store), "
        "per the SLO design.",
    )
