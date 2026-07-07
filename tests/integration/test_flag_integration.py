"""FLAG test-drift over a really-indexed package (reuses the resolved call/reference graph)."""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.flag import FlagEngine
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.store import InMemoryStore


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "tests").mkdir()
    (proj / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n", "utf-8")
    (proj / "tests" / "test_calc.py").write_text(
        "from proj.pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", "utf-8"
    )
    return proj


def _indexed(tmp_path: Path) -> InMemoryStore:
    store = InMemoryStore()
    index_package(_make_proj(tmp_path), repo_id="proj", store=store)
    return store


def test_tests_for_symbols_finds_the_exercising_test(tmp_path: Path) -> None:
    report = FlagEngine(_indexed(tmp_path)).tests_for_symbols(["proj.pkg.calc.add"]).to_dict()
    assert "proj.tests.test_calc.test_add" in {t["qualified_name"] for t in report["tests"]}


def test_tests_for_diff_maps_changed_lines_to_tests(tmp_path: Path) -> None:
    store = _indexed(tmp_path)
    # A diff touching add()'s body (line 2) -> changed_set -> add -> its tests.
    diff = (
        "--- a/proj/pkg/calc.py\n"
        "+++ b/proj/pkg/calc.py\n"
        "@@ -2 +2 @@\n"
        "-    return a + b\n"
        "+    return (a + b)\n"
    )
    report = FlagEngine(store).tests_for_diff(diff).to_dict()
    assert "proj.tests.test_calc.test_add" in {t["qualified_name"] for t in report["tests"]}


def test_suggest_tests_mcp_tool(tmp_path: Path) -> None:
    tools = CartogateTools(_indexed(tmp_path))
    out = dispatch(tools, "suggest_tests", {"symbols": ["proj.pkg.calc.add"]})
    assert out["count"] >= 1
    assert "proj.tests.test_calc.test_add" in {t["qualified_name"] for t in out["tests"]}
