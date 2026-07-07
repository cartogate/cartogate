"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from cartogate.schema.enums import Confidence, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node

MakeSymbol = Callable[..., Node]


@pytest.fixture
def make_symbol() -> MakeSymbol:
    """Factory for symbol nodes with sensible defaults, for store/engine tests."""

    def _make(
        qualified_name: str,
        *,
        signature: str | None = None,
        unit: str = "m.py",
        visibility: Visibility = Visibility.EXPORTED,
        content: str = "x",
        repo_id: str = "test",
        start_line: int = 1,
        end_line: int = 2,
        is_top_level: bool = True,
    ) -> Node:
        return Node.create(
            repo_id=repo_id,
            qualified_name=qualified_name,
            kind=NodeKind.SYMBOL,
            name=qualified_name.rsplit(".", 1)[-1],
            unit=unit,
            signature=signature,
            location=Location(path=unit, start_line=start_line, end_line=end_line),
            visibility=visibility,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            content_hash=content,
            is_top_level=is_top_level,
        )

    return _make

@pytest.fixture(autouse=True)
def _isolated_cartogate_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test gets its own CARTOGATE_HOME.

    The workspace registry (~/.cartogate/workspaces.json) is GLOBAL state: without this, any test
    that activates a workspace writes into the developer's real registry, and the deferred
    resolver's auto-connect rung reads it — tests would pass or fail depending on what daemons the
    HOST machine happens to be running.
    """
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path_factory.mktemp("gg-home")))
