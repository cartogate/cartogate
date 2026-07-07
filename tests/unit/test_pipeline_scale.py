"""§8.6 scale guards: the initial index does a single store rebuild (no quadratic blow-up),
and a trip-wire warns when an index crosses the size/time ceilings."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cartogate.extract import pipeline
from cartogate.extract.pipeline import (
    SCALE_TRIPWIRE_NODES,
    SCALE_TRIPWIRE_SECONDS,
    index_package,
    scale_warning,
)
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"


def test_index_does_a_single_store_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per-unit upsert rebuilds the whole graph on every call (O(units × N)); the bulk_load path
    # rebuilds once. Spy on the rebuild to prove the initial index triggers exactly ONE, no matter
    # how many files (units) there are — that is the quadratic being gone.
    calls = {"n": 0}
    original = InMemoryStore._build_derived

    def counting(units: object) -> object:
        calls["n"] += 1
        return original(units)  # type: ignore[arg-type]

    monkeypatch.setattr(InMemoryStore, "_build_derived", staticmethod(counting))

    store = InMemoryStore()
    result = index_package(FIXTURE_ROOT, repo_id="t", store=store)
    assert result.files_indexed > 1  # multi-file fixture: per-unit upsert would rebuild many times
    assert calls["n"] == 1  # ...but bulk_load does exactly one rebuild for the whole index


def test_scale_warning_silent_when_small() -> None:
    assert scale_warning(10, 5, 0.1) is None


def test_scale_warning_fires_on_node_ceiling() -> None:
    msg = scale_warning(SCALE_TRIPWIRE_NODES, 10, 0.1)
    assert msg is not None
    assert "§8.6" in msg and "nodes" in msg


def test_scale_warning_fires_on_time_ceiling() -> None:
    msg = scale_warning(10, 5, SCALE_TRIPWIRE_SECONDS)
    assert msg is not None
    assert "§8.6" in msg


def test_index_logs_warning_when_over_threshold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Force a tiny node ceiling so the real fixture index trips the trip-wire, and assert it logs.
    monkeypatch.setattr(pipeline, "SCALE_TRIPWIRE_NODES", 1)
    store = InMemoryStore()
    with caplog.at_level(logging.WARNING, logger="cartogate"):
        index_package(FIXTURE_ROOT, repo_id="t", store=store)
    assert any("index is large" in record.message for record in caplog.records)
