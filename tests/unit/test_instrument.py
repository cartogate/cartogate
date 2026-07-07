"""Gate tests for Section 0 — the decomposed instrumentation harness.

These assert the contract every later section relies on: each latency-sensitive
operation emits a span tagged with (phase, duration_ns, node_count, edge_count,
rss_bytes), and per-phase percentiles can be computed from a stream of spans.
Written before the implementation (TDD red), then implemented to green.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from cartogate.instrument import (
    MetricsAggregator,
    Phase,
    Span,
    SpanRecorder,
    percentile,
)


class FakeClock:
    """Deterministic monotonic clock: each call advances by a fixed step."""

    def __init__(self, start: int = 0, step: int = 1000) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> int:
        value = self._now
        self._now += self._step
        return value


def test_span_records_phase_duration_and_counts() -> None:
    # Arrange: deterministic clock (1000ns per tick) and fixed RSS sampler.
    clock = FakeClock(start=0, step=1000)
    recorder = SpanRecorder(clock=clock, rss_sampler=lambda: 4096)

    # Act
    with recorder.span(Phase.QUERY_TRAVERSAL, name="callers_of") as handle:
        handle.set_counts(node_count=12, edge_count=34)

    # Assert
    assert len(recorder.spans) == 1
    span = recorder.spans[0]
    assert span.phase is Phase.QUERY_TRAVERSAL
    assert span.name == "callers_of"
    assert span.duration_ns == 1000  # exactly one clock step elapsed
    assert span.node_count == 12
    assert span.edge_count == 34
    assert span.rss_bytes == 4096


def test_span_duration_is_monotonic_nonnegative_with_real_clock() -> None:
    recorder = SpanRecorder()  # real perf_counter_ns + real RSS sampler
    with recorder.span(Phase.LOAD_ATTACH):
        pass
    span = recorder.spans[0]
    assert span.duration_ns >= 0
    assert span.rss_bytes > 0  # a real process always has nonzero RSS


def test_phase_has_exactly_the_three_spec_phases() -> None:
    assert {p.value for p in Phase} == {"load_attach", "query_traversal", "resolution"}


def test_span_serializes_to_jsonl() -> None:
    captured: list[str] = []
    recorder = SpanRecorder(
        sink=lambda s: captured.append(s.to_json()),
        clock=FakeClock(step=500),
        rss_sampler=lambda: 2048,
    )
    with recorder.span(Phase.RESOLUTION, name="resolve") as handle:
        handle.set_counts(node_count=1, edge_count=2)

    assert len(captured) == 1
    payload = json.loads(captured[0])
    assert payload["phase"] == "resolution"
    assert payload["name"] == "resolve"
    assert payload["duration_ns"] == 500
    assert payload["node_count"] == 1
    assert payload["edge_count"] == 2
    assert payload["rss_bytes"] == 2048


def test_three_phase_op_yields_three_spans_and_p95() -> None:
    # A fake 3-phase operation (load -> query -> resolution) produces 3 spans.
    clock = FakeClock(start=0, step=1000)
    recorder = SpanRecorder(clock=clock, rss_sampler=lambda: 1)
    for phase in (Phase.LOAD_ATTACH, Phase.QUERY_TRAVERSAL, Phase.RESOLUTION):
        with recorder.span(phase):
            pass

    assert len(recorder.spans) == 3
    assert [s.phase for s in recorder.spans] == [
        Phase.LOAD_ATTACH,
        Phase.QUERY_TRAVERSAL,
        Phase.RESOLUTION,
    ]

    agg = MetricsAggregator()
    agg.extend(recorder.spans)
    # Each phase ran exactly once with a 1000ns step.
    assert agg.percentile(Phase.QUERY_TRAVERSAL, 95) == pytest.approx(1000.0)

    summary = agg.summary()
    assert summary[Phase.LOAD_ATTACH.value]["count"] == 1
    assert "p95" in summary[Phase.LOAD_ATTACH.value]


def test_percentile_linear_interpolation() -> None:
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 0) == pytest.approx(10.0)
    assert percentile(values, 100) == pytest.approx(50.0)
    assert percentile(values, 50) == pytest.approx(30.0)
    # p95 of 5 points sits between the 4th and 5th sample (linear interp).
    assert percentile(values, 95) == pytest.approx(48.0)


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1, 2, 3], 150)


def test_span_is_immutable() -> None:
    span = Span(
        phase=Phase.LOAD_ATTACH,
        name="x",
        duration_ns=1,
        node_count=0,
        edge_count=0,
        rss_bytes=1,
    )
    with pytest.raises(FrozenInstanceError):
        span.duration_ns = 2  # type: ignore[misc]
