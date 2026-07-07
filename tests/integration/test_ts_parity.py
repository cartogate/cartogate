"""Parity: the engines (blast_radius / find_references / FLAG) work over resolved TS edges."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.engine.flag import FlagEngine
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore


@pytest.fixture
def ts_store(tmp_path: Path) -> InMemoryStore:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "calc.ts").write_text(
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
        encoding="utf-8",
    )
    # A TS test file (recognised by the `.test.ts` convention) that exercises `add`.
    (proj / "calc.test.ts").write_text(
        'import { add } from "./calc";\n\ntest("adds", () => {\n  add(1, 2);\n});\n',
        encoding="utf-8",
    )
    (proj / "README.md").write_text("# Calc\n\nUse `add` to sum two numbers.\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(proj, repo_id="proj", store=store)
    return store


def test_blast_radius_finds_ts_callers(ts_store: InMemoryStore) -> None:
    out = CartogateTools(ts_store).blast_radius("proj.calc.add")
    assert out["found"] is True
    assert any("calc.test" in a["qualified_name"] for a in out["affected"])


def test_find_references_ts(ts_store: InMemoryStore) -> None:
    out = CartogateTools(ts_store).find_references("proj.calc.add")
    assert any("calc.test" in r["qualified_name"] for r in out["references"])


def test_suggest_tests_ts(ts_store: InMemoryStore) -> None:
    report = FlagEngine(ts_store).tests_for_symbols(["proj.calc.add"]).to_dict()
    assert any("calc.test" in t["qualified_name"] for t in report["tests"])


def test_doc_drift_ts(ts_store: InMemoryStore) -> None:
    report = FlagEngine(ts_store).docs_for_symbols(["proj.calc.add"]).to_dict()
    assert any(d["path"].endswith("README.md") for d in report["docs"])
