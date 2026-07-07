"""Decomposed instrumentation harness.

Every latency-sensitive operation is timed as a span split into the three phases
defined by the spec (§8.5): ``load_attach``, ``query_traversal``, ``resolution`` —
each tagged with node/edge count and RSS. Built first (Section 0) because every
later verification gate asserts that operations "emit a span with node count".
"""

from cartogate.instrument.metrics import MetricsAggregator, percentile
from cartogate.instrument.spans import (
    NULL_SPAN_HANDLE,
    NullSpanHandle,
    Phase,
    Span,
    SpanRecorder,
    jsonl_file_sink,
)

__all__ = [
    "NULL_SPAN_HANDLE",
    "MetricsAggregator",
    "NullSpanHandle",
    "Phase",
    "Span",
    "SpanRecorder",
    "jsonl_file_sink",
    "percentile",
]
