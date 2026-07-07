"""End-to-end Rust extraction over the sample_rust fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_rust"


def _index():
    store = InMemoryStore()
    # base = the crate root; `root_module="crate"` makes lib.rs the `crate` module and lines the
    # `crate::` paths up with the derived qnames (`crate.models.User`, …).
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_rust", store=store, base=FIXTURE_ROOT, index_docs=False
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"crate.models.User", "crate.models.Greeter", "crate.models.User.new",
            "crate.auth.authenticate", "crate.auth.validate", "crate.auth.make_user"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"crate", "crate.auth", "crate.models"} <= modules  # crate-rooted, no empty qname


def test_resolved_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("crate.auth.authenticate", "calls", "crate.auth.validate") in edges  # same-module
    assert ("crate.auth.make_user", "calls", "crate.models.User.new") in edges  # assoc call
    assert ("crate.auth", "imports", "crate.models.User") in edges  # in-repo use
    assert ("crate.models.User", "inherits", "crate.models.Greeter") in edges  # impl Trait for Type


def test_external_import_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "std::fmt" in externals  # the external use never produces an in-repo edge


def test_nodes_tagged_rust() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.RUST for n in syms)


def test_rust_duplicate_gate() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    fn = dispatch(tools, "check_duplicate",
                  {"signature": "pub fn validate(name: &str) -> bool", "language": "rust"})
    assert fn["blocked"] and fn["existing_qualified_name"] == "crate.auth.validate"
    ty = dispatch(tools, "check_duplicate", {"signature": "pub struct User", "language": "rust"})
    assert ty["blocked"] and ty["existing_qualified_name"] == "crate.models.User"
    assert not dispatch(tools, "check_duplicate",
                        {"signature": "fn brandnew()", "language": "rust"})["blocked"]


def test_python_and_rust_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "rs").mkdir()
    (tmp_path / "py" / "m.py").write_text("def make_user():\n    pass\n", encoding="utf-8")
    (tmp_path / "rs" / "lib.rs").write_text("pub fn make_user() {}\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    py = tools.check_duplicate("def make_user():", language="python")
    rs = tools.check_duplicate("fn make_user()", language="rust")
    assert py["blocked"] and rs["blocked"]
    assert py["existing_qualified_name"] != rs["existing_qualified_name"]  # no cross-collision
