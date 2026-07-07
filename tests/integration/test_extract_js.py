"""End-to-end JavaScript extraction over the sample_js fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_js"


def _index():
    store = InMemoryStore()
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_js", store=store, base=FIXTURE_ROOT, index_docs=False
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"models.User", "models.Base", "auth.authenticate", "auth.validate",
            "App.App", "service.login"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"models", "auth", "App", "service"} <= modules  # a JS file is a module


def test_resolved_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("auth.authenticate", "calls", "auth.validate") in edges  # same-file call
    assert ("auth.authenticate", "calls", "models.User") in edges  # new User()
    assert ("auth", "imports", "models.User") in edges  # ESM relative import (.js suffix)
    assert ("models.User", "inherits", "models.Base") in edges  # extends
    assert ("App.App", "references", "models.User") in edges  # <User/> JSX component reference
    assert ("service.login", "calls", "auth.authenticate") in edges  # CommonJS require resolution


def test_external_import_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "lodash" in externals  # bare specifier — external, no in-repo edge


def test_nodes_tagged_javascript() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.JAVASCRIPT for n in syms)


def test_js_duplicate_gate() -> None:
    # The gate's headline value is on top-level functions ("you re-implemented this util"), the same
    # case the TS suite asserts. (Classes carry a synthetic ``Name(bases)`` signature, so they match
    # only the same shape — not a concern here.)
    store, _ = _index()
    tools = CartogateTools(store)
    fn = dispatch(tools, "check_duplicate",
                  {"signature": "export function authenticate(name)", "language": "javascript"})
    assert fn["blocked"] and fn["existing_qualified_name"] == "auth.authenticate"
    assert not dispatch(tools, "check_duplicate",
                        {"signature": "function brandNew()", "language": "javascript"})["blocked"]


def test_python_ts_js_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "ts").mkdir()
    (tmp_path / "js").mkdir()
    (tmp_path / "py" / "m.py").write_text("def make_user():\n    pass\n", encoding="utf-8")
    (tmp_path / "ts" / "m.ts").write_text("export function make_user() {}\n", encoding="utf-8")
    (tmp_path / "js" / "m.js").write_text("export function make_user() {}\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    py = tools.check_duplicate("def make_user():", language="python")
    ts = tools.check_duplicate("function make_user()", language="typescript")
    js = tools.check_duplicate("function make_user()", language="javascript")
    assert py["blocked"] and ts["blocked"] and js["blocked"]
    names = {py["existing_qualified_name"], ts["existing_qualified_name"],
             js["existing_qualified_name"]}
    assert len(names) == 3  # all distinct — language folded into node identity


def test_js_typed_receiver_method_call_resolves(tmp_path: Path) -> None:
    # F-69 in JS: no type annotations, but a `const c = new Svc()` receiver still resolves its
    # method call (sound — the constructor names the type). Confirms JS rides the TS resolver.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "m.js").write_text(
        "class Svc {\n  run() {}\n}\n"
        "export function use() {\n  const c = new Svc();\n  c.run();\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(proj, repo_id="proj", store=store, base=proj, index_docs=False)
    assert ("m.use", "calls", "m.Svc.run") in _edges(result)  # base=proj → module is `m`
