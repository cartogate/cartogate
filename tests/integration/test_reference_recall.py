"""Reference recall: find_references / suggest_tests count subclass + import references too.

The recall gap measured in the value study (V3) was the query layer dropping ``inherits`` and
``imports`` edges — genuine references (a subclass depends on its base; an importer references
the symbol) that pyright counts. They resolve correctly through package re-exports already; the
fix is to include them in the "references" edge set (no precision cost — they are real).
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.flag import FlagEngine
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore


def _index(tmp_path: Path) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    # BaseCommand is defined in core and re-exported from the package's __init__.
    (pkg / "__init__.py").write_text("from pkg.core import BaseCommand\n", encoding="utf-8")
    (pkg / "core.py").write_text("class BaseCommand:\n    pass\n", encoding="utf-8")
    # Three referencers, each importing through the package alias (not pkg.core directly):
    (pkg / "user_call.py").write_text(
        "from pkg import BaseCommand\n\n\ndef make():\n    return BaseCommand()\n", encoding="utf-8"
    )
    (pkg / "user_sub.py").write_text(
        "from pkg import BaseCommand\n\n\nclass Derived(BaseCommand):\n    pass\n", encoding="utf-8"
    )
    # A test that exercises BaseCommand by subclassing it (a common test-double pattern).
    (pkg / "test_cmd.py").write_text(
        "from pkg import BaseCommand\n\n\nclass FakeCommand(BaseCommand):\n    pass\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path, index_docs=False)
    return store


def test_find_references_includes_subclasses_and_importers(tmp_path: Path) -> None:
    refs = CartogateTools(_index(tmp_path)).find_references("pkg.core.BaseCommand")
    names = {r["qualified_name"] for r in refs["references"]}
    assert "pkg.user_call.make" in names  # a call (was already found)
    assert "pkg.user_sub.Derived" in names  # a SUBCLASS — previously missed (inherits edge)
    assert "pkg.user_call" in names  # an IMPORTER — previously missed (imports edge)


def test_suggest_tests_finds_a_subclassing_test(tmp_path: Path) -> None:
    report = FlagEngine(_index(tmp_path)).tests_for_symbols(["pkg.core.BaseCommand"]).to_dict()
    tests = {t["qualified_name"] for t in report["tests"]}
    # test_cmd subclasses BaseCommand through the re-export -> it exercises it.
    assert any("test_cmd" in t for t in tests)
