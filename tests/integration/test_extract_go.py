"""End-to-end Go extraction over the sample_go fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_go"


def _index():
    store = InMemoryStore()
    # base = the module root (where go.mod lives), so directory == package and import paths line up.
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_go", store=store, base=FIXTURE_ROOT, index_docs=False
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"models.User", "models.Base", "models.Greeter", "models.NewUser",
            "models.User.Greet", "auth.Authenticate", "auth.MakeUser"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert modules == {"auth", "models"}  # one package module per directory


def test_resolved_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("auth.Authenticate", "calls", "auth.validate") in edges  # same-package
    assert ("auth.MakeUser", "calls", "models.NewUser") in edges  # cross-package selector call
    assert ("auth", "imports", "models") in edges  # in-repo package (go.mod prefix stripped)
    assert ("models.User", "inherits", "models.Base") in edges  # struct embedding


def test_external_import_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "fmt" in externals


def test_nodes_tagged_go() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.GO for n in syms)


def test_go_duplicate_gate() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    fn = dispatch(tools, "check_duplicate",
                  {"signature": "func NewUser(name string) *User", "language": "go"})
    assert fn["blocked"] and fn["existing_qualified_name"] == "models.NewUser"
    ty = dispatch(tools, "check_duplicate", {"signature": "type User struct{}", "language": "go"})
    assert ty["blocked"] and ty["existing_qualified_name"] == "models.User"
    assert not dispatch(tools, "check_duplicate",
                        {"signature": "func Brandnew()", "language": "go"})["blocked"]


def test_python_and_go_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "go").mkdir()
    (tmp_path / "go" / "go.mod").write_text("module ex\n", encoding="utf-8")
    (tmp_path / "py" / "m.py").write_text("def make_user():\n    pass\n", encoding="utf-8")
    (tmp_path / "go" / "u.go").write_text(
        "package go\nfunc make_user() {}\n", encoding="utf-8"
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    py = tools.check_duplicate("def make_user():", language="python")
    go = tools.check_duplicate("func make_user()", language="go")
    assert py["blocked"] and go["blocked"]
    assert py["existing_qualified_name"] != go["existing_qualified_name"]  # no cross-collision
