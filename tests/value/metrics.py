"""Metrics + the results sink for the value study (Layer 1).

Two responsibilities, kept dependency-light on purpose:

1. **Scoring** — precision/recall/F1 over sets, plus small descriptive stats. The latency
   percentile is delegated to the project's own :func:`cartogate.instrument.percentile`
   so the study and the SLO benchmark report numbers the same way.
2. **Recording** — a process-global collector that each value test appends a row to. A
   ``pytest_sessionfinish`` hook (see ``conftest.py``) *merges* the rows into
   ``evaluation/value_results.json`` so the deterministic layer (V2–V10) and the live A/B layer
   (V1) can each own their keys without clobbering the other's.

Nothing here imports pytest, so the scoring helpers stay usable from plain scripts too.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cartogate.instrument import percentile

#: The committed machine-readable results file (docs are generated from it).
REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_PATH = REPO_ROOT / "evaluation" / "value_results.json"
RESULTS_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Classification:
    """Precision/recall/F1 of a predicted set against a ground-truth set.

    All three are reported because the value claims trade off differently: the gates want
    high precision (a false block is costly), FLAG wants high recall (don't miss a stale
    doc/test), and we want to show Cartogate beating the baseline on whichever matters.
    """

    true_positive: int
    false_positive: int
    false_negative: int

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def classify(predicted: Iterable[Any], truth: Iterable[Any]) -> Classification:
    """Score a predicted set against ground truth (membership, order-independent)."""
    pred, true = set(predicted), set(truth)
    return Classification(
        true_positive=len(pred & true),
        false_positive=len(pred - true),
        false_negative=len(true - pred),
    )


def latency_summary_ns(durations_ns: Sequence[int]) -> dict[str, float]:
    """p50/p95/p99 (in milliseconds) for a list of nanosecond durations."""
    return {
        "p50_ms": percentile(durations_ns, 50) / 1e6,
        "p95_ms": percentile(durations_ns, 95) / 1e6,
        "p99_ms": percentile(durations_ns, 99) / 1e6,
        "n": len(durations_ns),
    }


def describe(values: Sequence[float]) -> dict[str, float]:
    """mean / stdev / min / max for a sample (stdev is 0.0 for n < 2)."""
    return {
        "mean": statistics.fmean(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


@dataclass
class ResultsCollector:
    """Process-global accumulator of hypothesis result rows for one pytest session."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(
        self,
        *,
        hypothesis: str,
        bucket: str,
        title: str,
        claim: str,
        metric: dict[str, Any],
        passed: bool,
        reproduce: str,
        notes: str = "",
    ) -> None:
        """Record one hypothesis' result (last write per id wins within a session)."""
        self.rows[hypothesis] = {
            "id": hypothesis,
            "bucket": bucket,
            "title": title,
            "claim": claim,
            "metric": metric,
            "passed": passed,
            "reproduce": reproduce,
            "notes": notes,
        }

    def flush(self, path: Path = RESULTS_PATH) -> None:
        """Merge the collected rows into ``path`` (creating it if absent).

        Merging (rather than overwriting) lets the live A/B layer keep its V1 row when the
        deterministic suite re-runs, and vice-versa.
        """
        if not self.rows:
            return
        existing = _load_results(path)
        existing.setdefault("hypotheses", {}).update(self.rows)
        existing["schema_version"] = RESULTS_SCHEMA_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_results(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": RESULTS_SCHEMA_VERSION, "hypotheses": {}}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


#: The single collector shared across the session (wired to a fixture in ``conftest.py``).
COLLECTOR = ResultsCollector()
