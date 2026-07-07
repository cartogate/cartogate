"""Section 5 — worst-case-scale benchmark + SLO confirmation (spec §8.4, §8.6).

Only the synchronous in-loop pre-write gate is latency-sensitive. In production that gate
is ``check_duplicate`` against a warm-resident store, so this benchmark builds a large warm
store and measures ``check_duplicate`` latency, asserting **p95 ≤ 50 ms**. It also profiles
the heavier ``blast_radius`` traversal (used before modifying exported symbols) with the
decomposed instrumentation, and records the §8.6 migration trip-wire reading (warm p95 +
RSS at scale) so a future store-migration decision has a baseline.

Indexing/extraction latency is explicitly OUT of the SLO budget, so the store is built by
direct upserts here rather than via the (resolution-heavy) extraction pipeline.
"""

from __future__ import annotations

import gc
import time

import pytest

from cartogate.engine.block import BlockEngine
from cartogate.instrument import MetricsAggregator, Phase, SpanRecorder, percentile
from cartogate.instrument.spans import default_rss_sampler
from cartogate.mcp.tools import CartogateTools
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore

#: Target scale for the worst-case store (symbols ~ functions in a large monorepo slice).
SCALE_SYMBOLS = 50_000
SCALE_UNITS = 50
#: The hard SLO (spec §8.4): synchronous in-loop gate p95.
SLO_P95_MS = 50.0
#: The alert band ceiling — a soft warning threshold below the hard hook timeout.
ALERT_P95_MS = 150.0


def _build_large_store(
    *, n_symbols: int, units: int, recorder: SpanRecorder | None = None
) -> InMemoryStore:
    store = InMemoryStore(recorder=recorder)
    per_unit = n_symbols // units
    for u in range(units):
        unit = f"pkg/m{u}.py"
        nodes: list[Node] = []
        for j in range(per_unit):
            idx = u * per_unit + j
            nodes.append(
                Node.create(
                    repo_id="bench",
                    qualified_name=f"pkg.m{u}.func_{idx}",
                    kind=NodeKind.SYMBOL,
                    name=f"func_{idx}",
                    unit=unit,
                    signature=f"def func_{idx}(a, b):",
                    location=Location(path=unit, start_line=j + 1, end_line=j + 2),
                    visibility=Visibility.EXPORTED,
                    provenance=Provenance.TREE_SITTER,
                    confidence=Confidence.EXTRACTED,
                    content_hash=str(idx),
                    is_top_level=True,  # module-level functions → in the duplicate index
                )
            )
        # A call chain within the unit so blast_radius has real edges to traverse.
        edges = [
            Edge(
                type=EdgeType.CALLS,
                src=nodes[j].id,
                dst=nodes[j - 1].id,
                provenance=Provenance.TREE_SITTER,
                confidence=Confidence.EXTRACTED,
            )
            for j in range(1, len(nodes))
        ]
        store.upsert_unit(unit, nodes, edges)
    return store


def _rss_mb() -> float:
    return default_rss_sampler() / (1024 * 1024)  # reuse the fork-safe sampler


@pytest.mark.benchmark
def test_in_loop_gate_p95_under_slo() -> None:
    store = _build_large_store(n_symbols=SCALE_SYMBOLS, units=SCALE_UNITS)
    engine = BlockEngine(store)

    # Warm-up (prime any lazy state; not measured).
    for i in range(500):
        engine.check_duplicate(f"def func_{i}(a, b):")

    iterations = 5000
    durations_ns: list[int] = []
    # Disable GC during the timed window so a generation-2 collection over the 50k nodes
    # can't masquerade as gate latency. All-hits (i % SCALE_SYMBOLS) is the pessimistic
    # direction: a hit pays the min()-over-matches cost; a miss returns immediately.
    gc.disable()
    try:
        for i in range(iterations):
            signature = f"def func_{i % SCALE_SYMBOLS}(a, b):"
            start = time.perf_counter_ns()
            engine.check_duplicate(signature)
            durations_ns.append(time.perf_counter_ns() - start)
    finally:
        gc.enable()

    p50 = percentile(durations_ns, 50) / 1e6
    p95 = percentile(durations_ns, 95) / 1e6
    p99 = percentile(durations_ns, 99) / 1e6

    print(
        f"\n[SLO] check_duplicate over {SCALE_SYMBOLS} symbols / {SCALE_UNITS} units "
        f"({iterations} calls): p50={p50:.4f}ms p95={p95:.4f}ms p99={p99:.4f}ms "
        f"| RSS={_rss_mb():.0f}MB"
    )
    print(
        "[SLO] decomposition: load_attach ~0 (warm-resident), resolution ~0 (index-time); "
        "the gate latency measured above is the indexed signature lookup. Trip-wire (sec 8.6): "
        "migrate only if a warm p95 exceeds budget OR RSS is unmanageable at the largest real repo."
    )

    assert p95 <= SLO_P95_MS, f"in-loop gate p95 {p95:.3f}ms exceeds SLO {SLO_P95_MS}ms"
    if p95 > ALERT_P95_MS:  # pragma: no cover - alerting band, not a hard failure
        pytest.fail(f"p95 {p95:.3f}ms is in the alert band (> {ALERT_P95_MS}ms)")


@pytest.mark.benchmark
def test_blast_radius_traversal_profile() -> None:
    recorder = SpanRecorder()
    store = _build_large_store(n_symbols=SCALE_SYMBOLS, units=SCALE_UNITS, recorder=recorder)
    tools = CartogateTools(store)

    # Profile blast_radius (a heavier, advisory traversal) on a spread of symbols.
    recorder.spans.clear()
    for u in range(SCALE_UNITS):
        tools.blast_radius(f"pkg.m{u}.func_{u * (SCALE_SYMBOLS // SCALE_UNITS)}", depth=3)

    agg = MetricsAggregator()
    agg.extend(recorder.spans)
    query_p95_ms = agg.percentile(Phase.QUERY_TRAVERSAL, 95) / 1e6
    summary = agg.summary()
    print(f"\n[PROFILE] blast_radius query_traversal p95={query_p95_ms:.4f}ms | {summary}")

    # Advisory query, not under the hard gate SLO — assert only that it stays well-behaved.
    assert query_p95_ms <= ALERT_P95_MS
