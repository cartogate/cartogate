"""Span recording: decomposed timing for latency-sensitive operations.

The spec (§8.5) requires every in-loop check to be timed as a span split into three
phases — ``load_attach``, ``query_traversal``, ``resolution`` — each tagged with the
node/edge count it touched and the process RSS at the time. Keeping these three apart
is what lets a future regression be localized to load vs. query vs. resolution without
a store swap.

Design notes:
- The clock is injectable (default :func:`time.perf_counter_ns`, a monotonic counter)
  so tests assert exact durations and the hot path stays allocation-light.
- RSS sampling is injectable so it can be stubbed in tests and, if it ever shows up on
  the latency budget, swapped for a cheaper sampler without touching call sites.
- :class:`Span` is a frozen dataclass rather than a Pydantic model: spans sit on the
  p95 ≤ 50 ms path, so we avoid per-span validation overhead.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TextIO

import psutil


class Phase(StrEnum):
    """The three decomposed timing phases of an in-loop check (spec §8.5)."""

    LOAD_ATTACH = "load_attach"
    QUERY_TRAVERSAL = "query_traversal"
    RESOLUTION = "resolution"


#: Signature of a clock: returns a monotonically non-decreasing nanosecond counter.
Clock = Callable[[], int]
#: Signature of an RSS sampler: returns the current resident-set size in bytes.
RssSampler = Callable[[], int]
#: Signature of a span sink: consumes a finished span (e.g. writes a JSONL line).
SpanSink = Callable[["Span"], None]


def default_rss_sampler() -> int:
    """Sample the current process resident-set size in bytes.

    ``psutil.Process()`` resolves ``os.getpid()`` at call time rather than caching a
    PID at import, so the reading stays correct if the process is forked after import
    (e.g. pytest-xdist workers, multiprocessing) — a cached parent PID would otherwise
    silently report the wrong RSS.
    """
    return int(psutil.Process().memory_info().rss)


@dataclass(frozen=True, slots=True)
class Span:
    """One finished, timed phase of an operation.

    Attributes mirror the spec's required tags so a span is self-describing in a log.
    """

    phase: Phase
    name: str
    duration_ns: int
    node_count: int
    edge_count: int
    rss_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable mapping (``phase`` rendered as its string value)."""
        data = asdict(self)
        data["phase"] = self.phase.value
        return data

    def to_json(self) -> str:
        """Render the span as a single compact JSON line (NDJSON-friendly)."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


class _SpanHandle:
    """Mutable handle yielded inside a ``span`` block so callers can tag node/edge counts.

    Counts are not known until the work is partway done (e.g. after a traversal expands),
    so they are set on the handle rather than passed up front.
    """

    __slots__ = ("phase", "name", "node_count", "edge_count")

    def __init__(self, phase: Phase, name: str) -> None:
        self.phase = phase
        self.name = name
        self.node_count = 0
        self.edge_count = 0

    def set_counts(self, *, node_count: int = 0, edge_count: int = 0) -> None:
        """Record how many nodes/edges this phase touched."""
        self.node_count = node_count
        self.edge_count = edge_count


class SpanRecorder:
    """Collects spans in memory and optionally streams them to a sink.

    Usage::

        recorder = SpanRecorder(sink=jsonl_sink)
        with recorder.span(Phase.QUERY_TRAVERSAL, name="callers_of") as h:
            ...                       # do the work
            h.set_counts(node_count=n, edge_count=e)
    """

    def __init__(
        self,
        *,
        sink: SpanSink | None = None,
        clock: Clock = time.perf_counter_ns,
        rss_sampler: RssSampler = default_rss_sampler,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._rss_sampler = rss_sampler
        self.spans: list[Span] = []

    @contextmanager
    def span(self, phase: Phase, *, name: str = "") -> Iterator[_SpanHandle]:
        """Time a block as a single span of ``phase``; record it on exit (even on error)."""
        handle = _SpanHandle(phase, name)
        start = self._clock()
        try:
            yield handle
        finally:
            duration = self._clock() - start
            span = Span(
                phase=phase,
                name=name,
                duration_ns=duration,
                node_count=handle.node_count,
                edge_count=handle.edge_count,
                rss_bytes=self._rss_sampler(),
            )
            self.spans.append(span)
            if self._sink is not None:
                self._sink(span)


class NullSpanHandle:
    """A no-op span handle for code paths with no recorder attached.

    Lets call sites use ``with span_or_null() as handle: handle.set_counts(...)`` uniformly
    whether or not instrumentation is active. Use :data:`NULL_SPAN_HANDLE` as the shared
    singleton with :func:`contextlib.nullcontext`.
    """

    def set_counts(self, *, node_count: int = 0, edge_count: int = 0) -> None:
        return None


#: Shared no-op handle (see :class:`NullSpanHandle`).
NULL_SPAN_HANDLE = NullSpanHandle()


def jsonl_file_sink(stream: TextIO) -> SpanSink:
    """Build a sink that appends each span to ``stream`` as one JSON line.

    The returned sink is called from inside :meth:`SpanRecorder.span`'s ``finally``
    block, so any exception raised by ``stream.write`` propagates to the caller and
    would mask an exception still in flight from the timed body. Callers that cannot
    tolerate that should wrap the stream in a buffer that does not raise on write, or
    pass a sink that swallows/forwards I/O errors itself.
    """

    def _sink(span: Span) -> None:
        stream.write(span.to_json() + "\n")

    return _sink
