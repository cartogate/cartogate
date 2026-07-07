"""End-to-end C++ extraction over the sample_cpp fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_cpp"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: a .hpp/.cpp pair of the same stem (user.hpp/user.cpp) collapse to one
    # module, so the class (header) and its out-of-line methods (source) share module `user`.
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_cpp", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"user.Base", "user.User", "user.User.isActive", "user.makeUser"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"user", "test_user"} <= modules


def test_defines_edges() -> None:
    edges = _edges(_index()[1])
    assert ("user", "defines", "user.User") in edges  # module -> class
    assert ("user.User", "defines", "user.User.isActive") in edges  # class -> out-of-line method


def test_resolved_edges() -> None:
    edges = _edges(_index()[1])
    assert ("user.User", "inherits", "user.Base") in edges
    assert ("user.User.isActive", "calls", "user.validate") in edges  # unqualified in a method
    assert ("user.makeUser", "calls", "user.User") in edges  # new User()


def test_external_include_becomes_external_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "string" in externals  # #include <string>


def test_nodes_tagged_cpp() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.CPP for n in syms)


def test_cpp_duplicate_gate_blocks_existing_class() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "class User", "language": "cpp"})
    assert hit["blocked"] is True and hit["existing_qualified_name"] == "user.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "class Brandnew", "language": "cpp"})["blocked"] is False


def test_overloaded_method_call_is_unresolved(tmp_path: Path) -> None:
    (tmp_path / "calc.hpp").write_text(
        "class Calc {\npublic:\n  int add(int a);\n  int add(int a, int b);\n"
        "  int use();\n};\n",
        encoding="utf-8",
    )
    (tmp_path / "calc.cpp").write_text(
        '#include "calc.hpp"\n'
        "int Calc::add(int a) { return a; }\n"
        "int Calc::add(int a, int b) { return a + b; }\n"
        "int Calc::use() { Calc c; return c.add(1); }\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="ov", store=store, base=tmp_path, index_docs=False)
    by = {n.id: n for n in result.nodes}
    calls = {(by[e.src].qualified_name, by[e.dst].qualified_name)
             for e in result.edges if e.type.value == "calls"}
    # `c.add(1)` is overloaded -> left unresolved (no arg-type inference), so no edge to Calc.add.
    assert not any(dst == "calc.Calc.add" for _, dst in calls)


def test_cpp_and_c_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "c").mkdir()
    (tmp_path / "cpp").mkdir()
    (tmp_path / "c" / "user.c").write_text("struct User { int x; };\n", encoding="utf-8")
    (tmp_path / "cpp" / "user.cpp").write_text("class User { int x; };\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    c = tools.check_duplicate("struct User", language="c")
    cpp = tools.check_duplicate("class User", language="cpp")
    assert c["blocked"] and c["existing_qualified_name"] == "c.user.User"
    assert cpp["blocked"] and cpp["existing_qualified_name"] == "cpp.user.User"
