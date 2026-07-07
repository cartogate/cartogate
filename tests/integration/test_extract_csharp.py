"""End-to-end C# extraction over the sample_csharp fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import EdgeType, Language, NodeKind
from cartogate.schema.signature import normalize_signature
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_csharp"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: each ``.cs`` file is its own module, so qnames are file-based
    # (``Models.User``); the resolver reads the in-file ``namespace``/``using`` to bind references.
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_csharp", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"Models.Base", "Models.User", "Models.User.IsActive",
            "Services.AuthService", "Services.AuthService.Authenticate"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"Models", "Services", "AuthServiceTests"} <= modules


def test_defines_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("Models", "defines", "Models.User") in edges  # module -> type
    assert ("Services.AuthService", "defines", "Services.AuthService.Authenticate") in edges


def test_resolved_edges() -> None:
    _, result = _index()
    edges = _edges(result)
    assert ("Services.AuthService.Make", "calls", "Models.User") in edges  # new User()
    assert ("Services.AuthService.Authenticate", "calls",
            "Services.AuthService.Validate") in edges  # same-class call
    assert ("Services.AuthService.Authenticate", "calls",
            "Models.User.IsActive") in edges  # declared-receiver _user.IsActive()
    assert ("Models.User", "inherits", "Models.Base") in edges


def test_external_using_becomes_external_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "System" in externals  # `using System;` -> external package node


def test_nodes_tagged_csharp() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.CSHARP for n in syms)


def test_csharp_duplicate_gate_blocks_existing_type() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "class User {}", "language": "csharp"})
    assert hit["blocked"] is True
    assert hit["existing_qualified_name"] == "Models.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "class Brandnew {}", "language": "csharp"})["blocked"] is False


def test_overloaded_methods_are_distinct_nodes_by_type(tmp_path: Path) -> None:
    (tmp_path / "Calc.cs").write_text(
        "namespace App {\n  public class Calc {\n"
        "    public int Add(int a, int b) { return a + b; }\n"
        "    public double Add(double a, double b) { return a + b; }\n  }\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="ov", store=store, base=tmp_path, index_docs=False)
    adds = [n for n in result.nodes if n.qualified_name == "Calc.Calc.Add"]
    assert len(adds) == 2 and len({n.id for n in adds}) == 2
    assert {normalize_signature(n.signature, Language.CSHARP) for n in adds} == {
        "Add(int,int)", "Add(double,double)"
    }


def test_overloaded_call_is_unresolved(tmp_path: Path) -> None:
    (tmp_path / "Calc.cs").write_text(
        "namespace App {\n  public class Calc {\n"
        "    private int HelperA(int a) { return a; }\n"
        "    public int Add(int a, int b) { return HelperA(a); }\n"
        "    public double Add(double a, double b) { return a; }\n"
        "    public int Use(int a) { return Add(a, a); }\n  }\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="ov", store=store, base=tmp_path, index_docs=False)
    by = {n.id: n for n in result.nodes}
    calls = {(by[e.src].qualified_name, by[e.dst].qualified_name)
             for e in result.edges if e.type is EdgeType.CALLS}
    # A non-overloaded call resolves...
    assert ("Calc.Calc.Add", "Calc.Calc.HelperA") in calls
    # ...but overloaded ``Add(a, a)`` is left unresolved (no arg-type inference) — no wrong edge.
    assert not any(dst == "Calc.Calc.Add" for _, dst in calls)


def test_csharp_and_python_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "py").mkdir()
    (tmp_path / "cs").mkdir()
    (tmp_path / "py" / "m.py").write_text("def user():\n    pass\n", encoding="utf-8")
    (tmp_path / "cs" / "User.cs").write_text(
        "namespace Cs { public class User { } }\n", encoding="utf-8"
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    py = tools.check_duplicate("def user():", language="python")
    cs = tools.check_duplicate("class User {}", language="csharp")
    assert py["blocked"] and py["existing_qualified_name"] == "py.m.user"
    assert cs["blocked"] and cs["existing_qualified_name"] == "cs.User.User"
