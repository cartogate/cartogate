"""Percentile aggregation over recorded spans.

Only the synchronous in-loop pre-write gate is held to the p95 ≤ 50 ms SLO (spec §8.4),
so we aggregate durations per :class:`~cartogate.instrument.spans.Phase` and expose
p50/p95/p99. No numpy dependency — the percentile is computed with linear interpolation
between closest ranks (the same method numpy calls ``"linear"``), which is stable for
the small-to-medium span counts a gate produces.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from cartogate.instrument.spans import Phase, Span


def percentile(values: Iterable[float], p: float) -> float:
    """Return the ``p``-th percentile (0–100) of ``values`` via linear interpolation.

    Args:
        values: A non-empty iterable of numbers.
        p: Percentile rank in the inclusive range ``[0, 100]``.

    Raises:
        ValueError: If ``values`` is empty or ``p`` is outside ``[0, 100]``.
    """
    if not 0 <= p <= 100:
        raise ValueError(f"percentile p must be in [0, 100], got {p}")
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of empty sequence is undefined")
    if len(ordered) == 1:
        return float(ordered[0])

    # Rank position on a 0..n-1 index scale, then interpolate between neighbours.
    rank = (p / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


class MetricsAggregator:
    """Accumulates span durations per phase and computes percentile summaries."""

    #: Percentiles reported by :meth:`summary`.
    REPORTED_PERCENTILES = (50, 95, 99)

    def __init__(self) -> None:
        self._durations_ns: dict[Phase, list[int]] = defaultdict(list)

    def add(self, span: Span) -> None:
        """Record one span's duration under its phase."""
        self._durations_ns[span.phase].append(span.duration_ns)

    def extend(self, spans: Iterable[Span]) -> None:
        """Record many spans."""
        for span in spans:
            self.add(span)

    def count(self, phase: Phase) -> int:
        """Number of spans recorded for ``phase``."""
        return len(self._durations_ns.get(phase, ()))

    def percentile(self, phase: Phase, p: float) -> float:
        """Return the ``p``-th percentile of recorded durations (ns) for ``phase``.

        Raises:
            ValueError: If no spans were recorded for ``phase``.
        """
        durations = self._durations_ns.get(phase)
        if not durations:
            raise ValueError(f"no spans recorded for phase {phase.value!r}")
        return percentile(durations, p)

    def summary(self) -> dict[str, dict[str, float | int]]:
        """Per-phase summary keyed by phase value: count + p50/p95/p99 (ns).

        Only phases that have at least one recorded span appear in the result; phases
        with no data are omitted entirely (never reported with a zero count). This is
        what keeps :func:`percentile` from being called on an empty list here.
        """
        result: dict[str, dict[str, float | int]] = {}
        for phase, durations in self._durations_ns.items():
            entry: dict[str, float | int] = {"count": len(durations)}
            for p in self.REPORTED_PERCENTILES:
                entry[f"p{p}"] = percentile(durations, p)
            result[phase.value] = entry
        return result
