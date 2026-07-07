"""End-to-end Java extraction over the sample_java fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import EdgeType, Language, NodeKind
from cartogate.schema.signature import normalize_signature
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_java"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: it is the Java *source root*, so package == directory path and the
    # `import app.models.User` FQNs line up with the indexed qualified names.
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_java", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"app.models.Base", "app.models.User", "app.models.Greeter",
            "app.auth.Auth", "app.auth.Auth.authenticate", "app.models.User.who"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert modules == {"app.auth", "app.models"}  # one package module per directory (shared)


def test_defines_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("app.models", "defines", "app.models.User") in edges  # package -> class
    assert ("app.auth.Auth", "defines", "app.auth.Auth.authenticate") in edges  # class -> method


def test_resolved_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("app.auth.Auth.makeUser", "calls", "app.models.User") in edges  # new User()
    assert ("app.auth.Auth.authenticate", "calls", "app.auth.Auth.validate") in edges  # same class
    assert ("app.auth", "imports", "app.models.User") in edges
    assert ("app.models.User", "inherits", "app.models.Base") in edges
    assert ("app.models.User", "inherits", "app.models.Greeter") in edges


def test_external_import_becomes_external_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "java" in externals  # java.util.List → external package node


def test_nodes_tagged_java() -> None:
    _, result = _index()
    java_syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert java_syms and all(n.language is Language.JAVA for n in java_syms)


def test_java_duplicate_gate_blocks_existing_type() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "class User {}", "language": "java"})
    assert hit["blocked"] is True
    assert hit["existing_qualified_name"] == "app.models.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "class Brandnew {}", "language": "java"})["blocked"] is False


def test_overloaded_methods_are_distinct_nodes_by_type(tmp_path: Path) -> None:
    pkg = tmp_path / "app"
    pkg.mkdir(parents=True)
    (pkg / "Calc.java").write_text(
        "package app;\npublic class Calc {\n"
        "  int add(int a, int b) { return a+b; }\n"
        "  double add(double a, double b) { return a+b; }\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="ov", store=store, base=tmp_path, index_docs=False)
    adds = [n for n in result.nodes if n.qualified_name == "app.Calc.add"]
    # Java overloads are distinguished by parameter type → distinct nodes (distinct ids despite the
    # shared qname), each carrying its by-type signature.
    assert len(adds) == 2
    assert len({n.id for n in adds}) == 2
    assert {normalize_signature(n.signature, Language.JAVA) for n in adds} == {
        "add(int,int)",
        "add(double,double)",
    }


def test_overloaded_call_is_unresolved_and_calls_are_per_overload(tmp_path: Path) -> None:
    pkg = tmp_path / "app"
    pkg.mkdir(parents=True)
    (pkg / "Calc.java").write_text(
        "package app;\npublic class Calc {\n"
        "  int helperA(int a) { return a; }\n"
        "  int helperB(int a) { return a; }\n"
        "  int add(int a, int b) { return helperA(a); }\n"
        "  double add(double a, double b) { return helperB(0); }\n"
        "  int use(int a) { return add(a, a); }\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="ov", store=store, base=tmp_path, index_docs=False)
    by = {n.id: n for n in result.nodes}

    def sig(node_id: str) -> str:
        n = by[node_id]
        return normalize_signature(n.signature, Language.JAVA) if n.signature else n.qualified_name

    calls = {
        (sig(e.src), by[e.dst].qualified_name) for e in result.edges if e.type is EdgeType.CALLS
    }
    # Each overload sources its OWN call (not mis-attributed to the other via the shared qname)...
    assert ("add(int,int)", "app.Calc.helperA") in calls
    assert ("add(double,double)", "app.Calc.helperB") in calls
    # ...and the overloaded call `add(a, a)` in use() is left UNRESOLVED — can't pick an overload
    # without arg-type inference, so no (wrong) edge to app.Calc.add.
    assert not any(dst == "app.Calc.add" for _, dst in calls)


def test_python_and_java_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "jv").mkdir()
    (tmp_path / "py" / "m.py").write_text("def user():\n    pass\n", encoding="utf-8")
    (tmp_path / "jv" / "User.java").write_text(
        "package jv;\npublic class User {}\n", encoding="utf-8"
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    # The same class name in each language resolves to its OWN symbol — no cross-language collision.
    py = tools.check_duplicate("def user():", language="python")
    jv = tools.check_duplicate("class User {}", language="java")
    assert py["blocked"] and py["existing_qualified_name"] == "py.m.user"
    assert jv["blocked"] and jv["existing_qualified_name"] == "jv.User"
