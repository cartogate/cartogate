"""End-to-end TypeScript structural extraction + the per-language duplicate gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate.engine.block import BlockEngine
from cartogate.extract.pipeline import index_package
from cartogate.extract.scip_emit import emit_scip
from cartogate.schema.enums import EdgeType, Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_ts"
REPO = "sample_ts"


class _Indexed:
    def __init__(self, result, store: InMemoryStore) -> None:
        self.result = result
        self.store = store
        self._by_id = {n.id: n for n in result.nodes}

    def qnames(self, kind: NodeKind) -> set[str]:
        return {n.qualified_name for n in self.result.nodes if n.kind is kind}

    def edges_of(self, edge_type: EdgeType) -> set[tuple[str, str]]:
        return {
            (self._by_id[e.src].qualified_name, self._by_id[e.dst].qualified_name)
            for e in self.result.edges
            if e.type is edge_type
        }


@pytest.fixture(scope="module")
def indexed() -> _Indexed:
    store = InMemoryStore()
    result = index_package(FIXTURE_ROOT, repo_id=REPO, store=store, index_docs=False)
    return _Indexed(result, store)


def test_symbol_nodes(indexed: _Indexed) -> None:
    assert indexed.qnames(NodeKind.SYMBOL) == {
        "sample_ts.models.Base",
        "sample_ts.models.Base.greet",
        "sample_ts.models.User",
        "sample_ts.models.User.constructor",
        "sample_ts.models.User.who",
        "sample_ts.auth.authenticate",
        "sample_ts.auth.validate",
        "sample_ts.auth.makeUser",
    }


def test_module_nodes_collapse_index_barrel(indexed: _Indexed) -> None:
    # index.ts collapses to the package qname, like Python's __init__.py.
    assert indexed.qnames(NodeKind.MODULE) == {"sample_ts", "sample_ts.auth", "sample_ts.models"}


def test_defines_edges(indexed: _Indexed) -> None:
    assert indexed.edges_of(EdgeType.DEFINES) == {
        ("sample_ts.models", "sample_ts.models.Base"),
        ("sample_ts.models", "sample_ts.models.User"),
        ("sample_ts.models.Base", "sample_ts.models.Base.greet"),
        ("sample_ts.models.User", "sample_ts.models.User.constructor"),
        ("sample_ts.models.User", "sample_ts.models.User.who"),
        ("sample_ts.auth", "sample_ts.auth.authenticate"),
        ("sample_ts.auth", "sample_ts.auth.validate"),
        ("sample_ts.auth", "sample_ts.auth.makeUser"),
    }


def test_resolved_edges(indexed: _Indexed) -> None:
    # Cross-file name resolution: a same-file call, a `new X()` across an import, the import
    # itself bound to the imported class, an external package, and class inheritance.
    assert ("sample_ts.auth.authenticate", "sample_ts.auth.validate") in indexed.edges_of(
        EdgeType.CALLS
    )
    assert ("sample_ts.auth.makeUser", "sample_ts.models.User") in indexed.edges_of(EdgeType.CALLS)
    assert ("sample_ts.auth", "sample_ts.models.User") in indexed.edges_of(EdgeType.IMPORTS)
    assert ("sample_ts.auth", "lodash") in indexed.edges_of(EdgeType.IMPORTS)  # external package
    assert ("sample_ts.models.User", "sample_ts.models.Base") in indexed.edges_of(EdgeType.INHERITS)


def test_scip_index_labels_typescript(indexed: _Indexed, tmp_path: Path) -> None:
    # SCIP output must carry the real language (was hardcoded "python"): document language and
    # the moniker scheme both reflect TypeScript.
    out = tmp_path / "index.scip.json"
    emit_scip(indexed.result, out, repo_id=REPO)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert {d["language"] for d in payload["documents"]} == {"typescript"}
    monikers = [s["symbol"] for d in payload["documents"] for s in d["symbols"]]
    assert monikers and all(m.startswith("scip-typescript ") for m in monikers)


def test_nodes_tagged_typescript(indexed: _Indexed) -> None:
    # Every node — including the external `lodash` package — carries the TypeScript language tag.
    assert {n.language for n in indexed.result.nodes} == {Language.TYPESCRIPT}


def test_ts_duplicate_is_blocked(indexed: _Indexed) -> None:
    block = BlockEngine(indexed.store).check_duplicate(
        "function authenticate(name, pwd)", Language.TYPESCRIPT
    )
    assert block.blocked is True
    assert block.existing_qualified_name == "sample_ts.auth.authenticate"


def test_overloaded_methods_are_one_symbol(tmp_path: Path) -> None:
    # TypeScript overloads declare the same callable several times; they are ONE symbol and
    # must not collide on node id (which would crash indexing).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "o.ts").write_text(
        "export class C {\n"
        "  f(a: number): void;\n"
        "  f(a: string): void;\n"
        "  f(a: unknown): void {}\n"
        "}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(proj, repo_id="proj", store=store, index_docs=False)
    f_nodes = [n for n in result.nodes if n.qualified_name == "proj.o.C.f"]
    assert len(f_nodes) == 1  # one node despite three declarations


def test_python_and_typescript_do_not_cross_collide(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "py").mkdir(parents=True)
    (proj / "ts").mkdir()
    (proj / "py" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (proj / "ts" / "calc.ts").write_text(
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(proj, repo_id="proj", store=store, index_docs=False)

    # Same normalized signature add(a,b) in both languages, but distinct node ids...
    add_ids = {n.id for n in result.nodes if n.name == "add"}
    assert len(add_ids) == 2
    # ...and the gate does not flag one language's add as a duplicate of the other's.
    engine = BlockEngine(store)
    assert engine.check_duplicate("def add(a, b):", Language.PYTHON).blocked is True
    assert engine.check_duplicate("function add(a, b)", Language.TYPESCRIPT).blocked is True
    # A Python add must not be reported as duplicating the *TypeScript* add (or vice versa):
    py_hit = engine.check_duplicate("def add(a, b):", Language.PYTHON)
    ts_hit = engine.check_duplicate("function add(a, b)", Language.TYPESCRIPT)
    assert py_hit.existing_qualified_name == "proj.py.calc.add"
    assert ts_hit.existing_qualified_name == "proj.ts.calc.add"
