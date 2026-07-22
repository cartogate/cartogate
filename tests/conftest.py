"""Shared test fixtures and helpers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cartogate.schema.enums import Confidence, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node


def git_cmd(repo: Path, *args: str) -> None:
    """Run a git command in ``repo`` — the ONE shared helper for git-backed tests.

    Consolidation target (gate field evidence 2026-07-17): 18 test files had grown their own
    identical ``_git`` copies; new tests import this instead of adding a 19th.
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def free_port() -> int:
    """An OS-assigned free TCP port (E2E servers; collision-proof across runs)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_file(repo: Path, rel: str, body: str) -> None:
    """Write ``body`` at ``repo/rel``, creating parents — the ONE shared helper
    (the gate refused a second ``_write`` copy, 2026-07-20)."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def init_git_repo(repo: Path) -> None:
    """``git init`` + a throwaway committer identity, so commits work on any machine."""
    git_cmd(repo, "init", "-q")
    git_cmd(repo, "config", "user.email", "t@t")
    git_cmd(repo, "config", "user.name", "T")


def write_contract(repo: Path, data: dict[str, Any]) -> Path:
    """Write a contract JSON file for `cartogate task declare` tests — the ONE shared copy
    (the duplicate gate blocked the second one, 2026-07-18)."""
    p = repo / "contract.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p

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
