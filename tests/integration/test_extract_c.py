"""End-to-end C extraction over the sample_c fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_c"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: each .c/.h file is its own module; a .h and its .c with the same
    # stem (user.h/user.c) share one module (`user`).
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_c", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"user.User", "user.create_user", "auth.authenticate", "auth.validate"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"user", "auth", "test_auth"} <= modules  # user.h + user.c collapse to one `user`


def test_defines_edges() -> None:
    edges = _edges(_index()[1])
    assert ("user", "defines", "user.create_user") in edges
    assert ("auth", "defines", "auth.authenticate") in edges


def test_resolved_edges() -> None:
    edges = _edges(_index()[1])
    assert ("auth.authenticate", "calls", "auth.validate") in edges  # same-TU static call
    assert ("auth.authenticate", "calls", "user.create_user") in edges  # cross-file global call
    assert ("auth.authenticate", "references", "user.User") in edges  # struct param type
    assert ("auth", "imports", "user") in edges  # #include "user.h"


def test_external_include_becomes_external_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "stdlib" in externals  # #include <stdlib.h>


def test_nodes_tagged_c() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.C for n in syms)


def test_c_duplicate_gate_blocks_existing_type_and_function() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "struct User", "language": "c"})
    assert hit["blocked"] is True and hit["existing_qualified_name"] == "user.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "struct Brandnew", "language": "c"})["blocked"] is False


def test_static_not_visible_cross_file(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text(
        "static int helper(int x) { return x; }\n"
        "int use_a(int x) { return helper(x); }\n",
        encoding="utf-8",
    )
    (tmp_path / "b.c").write_text(
        "int use_b(int x) { return helper(x); }\n", encoding="utf-8"
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="st", store=store, base=tmp_path, index_docs=False)
    by = {n.id: n for n in result.nodes}
    calls = {(by[e.src].qualified_name, by[e.dst].qualified_name)
             for e in result.edges if e.type.value == "calls"}
    assert ("a.use_a", "a.helper") in calls  # same-file static call resolves
    # b.use_b's call to `helper` (a's static) does NOT resolve — no cross-file edge to a.helper.
    assert not any(src == "b.use_b" for src, _ in calls)


def test_c_and_python_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "c").mkdir()
    (tmp_path / "py" / "m.py").write_text("def user():\n    pass\n", encoding="utf-8")
    (tmp_path / "c" / "user.c").write_text("struct User { int x; };\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    py = tools.check_duplicate("def user():", language="python")
    c = tools.check_duplicate("struct User", language="c")
    assert py["blocked"] and py["existing_qualified_name"] == "py.m.user"
    assert c["blocked"] and c["existing_qualified_name"] == "c.user.User"
