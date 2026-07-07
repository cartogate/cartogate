"""Fixtures for the value study and the hook that persists its results.

The ``pytest_sessionfinish`` hook flushes the collected hypothesis rows to
``evaluation/value_results.json`` once, at the end of the session — but only when value tests
actually ran (so a normal ``pytest`` run, which deselects ``-m value``, never touches the
file).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

from .metrics import COLLECTOR

FIXTURES = Path(__file__).parent / "fixtures"
#: Cartogate's own source — a real OOP Python+TS tree used for scale/latency measurement.
SELF_SRC = Path(__file__).resolve().parents[2] / "src" / "cartogate"

IndexFn = Callable[..., CartogateTools]


@pytest.fixture
def index_to_tools() -> IndexFn:
    """Index a package directory and return a ready :class:`CartogateTools` over it."""

    def _index(
        root: Path, *, repo_id: str = "value", resolve: bool = True, index_docs: bool = True
    ) -> CartogateTools:
        store = InMemoryStore()
        index_package(root, repo_id=repo_id, store=store, resolve=resolve, index_docs=index_docs)
        return CartogateTools(store)

    return _index


@pytest.fixture
def fixture_tools(index_to_tools: IndexFn) -> Callable[[str], CartogateTools]:
    """Index one of the crafted labeled fixtures by name (``tests/value/fixtures/<name>``)."""

    def _load(name: str) -> CartogateTools:
        return index_to_tools(FIXTURES / name, repo_id=name)

    return _load


@pytest.fixture
def fixture_store() -> Callable[[str], InMemoryStore]:
    """Index one of the crafted fixtures and return the raw store (for engine-level checks)."""

    def _load(name: str) -> InMemoryStore:
        store = InMemoryStore()
        index_package(FIXTURES / name, repo_id=name, store=store)
        return store

    return _load


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Persist recorded value-study rows to evaluation/value_results.json (merge, don't clobber)."""
    COLLECTOR.flush()
