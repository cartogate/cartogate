"""Tiny set-scoring helpers (kept local so the realstudy doesn't import from tests/)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Score:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def score(predicted: Iterable[Any], truth: Iterable[Any]) -> Score:
    pred, true = set(predicted), set(truth)
    return Score(tp=len(pred & true), fp=len(pred - true), fn=len(true - pred))


def add(a: Score, b: Score) -> Score:
    return Score(tp=a.tp + b.tp, fp=a.fp + b.fp, fn=a.fn + b.fn)
