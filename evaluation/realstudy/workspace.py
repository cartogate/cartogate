"""Build a flat workspace from the corpus snapshot so resolution + coverage line up.

The pinned corpus uses a ``src/`` layout (``src/click`` + top-level ``tests/``). Indexed as-is,
the tests' ``import click`` resolves to the *installed* click, not the snapshot, so there are
no test→symbol edges. We fix that by copying the package and its tests into a temp dir as
**siblings** (``<work>/click`` + ``<work>/tests``) and indexing with ``base=<work>`` — now
``import click`` resolves within the project and qualified names are clean (``click.…`` /
``tests.…``). The same flat dir is what pyright and coverage run against, so every oracle keys
on identical paths.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.schema.nodes import Node
from cartogate.store import InMemoryStore


@dataclass
class Workspace:
    root: Path  # the flat workspace dir
    package: str  # e.g. "click"
    package_dir: Path  # <root>/click
    tests_dir: Path  # <root>/tests
    store: InMemoryStore
    tools: CartogateTools
    nodes: tuple[Node, ...]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def build_workspace(snapshot_pkg: Path, snapshot_tests: Path, package: str) -> Workspace:
    """Copy ``package`` + ``tests`` into a flat temp dir, index it, and return the handle."""
    root = Path(tempfile.mkdtemp(prefix=f"realstudy_{package}_"))
    package_dir = root / package
    tests_dir = root / "tests"
    shutil.copytree(snapshot_pkg, package_dir)
    shutil.copytree(snapshot_tests, tests_dir)

    store = InMemoryStore()
    result = index_package(root, repo_id=package, store=store, base=root, resolve=True,
                           index_docs=False)
    return Workspace(
        root=root,
        package=package,
        package_dir=package_dir,
        tests_dir=tests_dir,
        store=store,
        tools=CartogateTools(store),
        nodes=tuple(result.nodes),
    )
