"""End-to-end Swift extraction over the sample_swift fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_swift"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: each .swift file is its own module; types/functions resolve repo-wide
    # by name (Swift's flat module namespace needs no imports between files).
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_swift", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"Models.Base", "Models.User", "Models.User.isActive", "Models.User.greet",
            "Service.AuthService", "Service.validate"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"Models", "Service", "AuthServiceTests"} <= modules


def test_defines_edges() -> None:
    edges = _edges(_index()[1])
    assert ("Models", "defines", "Models.User") in edges  # module -> class
    assert ("Models.User", "defines", "Models.User.greet") in edges  # extension method -> the type
    assert ("Service", "defines", "Service.validate") in edges  # module -> top-level function


def test_resolved_edges() -> None:
    edges = _edges(_index()[1])
    assert ("Models.User", "inherits", "Models.Base") in edges
    assert ("Models.User", "inherits", "Models.Logger") in edges  # protocol conformance
    assert ("Service.AuthService.authenticate", "calls",
            "Models.User.isActive") in edges  # declared-receiver across files
    assert ("Service.AuthService.authenticate", "calls",
            "Service.validate") in edges  # repo-wide top-level function
    assert ("Service.AuthService.make", "calls", "Models.User") in edges  # User(name:) initializer


def test_external_import_becomes_external_node() -> None:
    _, result = _index()
    externals = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert "Foundation" in externals  # `import Foundation`


def test_nodes_tagged_swift() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.SWIFT for n in syms)


def test_swift_duplicate_gate_blocks_existing_type() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "class User", "language": "swift"})
    assert hit["blocked"] is True and hit["existing_qualified_name"] == "Models.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "class Brandnew", "language": "swift"})["blocked"] is False


def test_swift_and_kotlin_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "sw").mkdir()
    (tmp_path / "kt").mkdir()
    (tmp_path / "sw" / "User.swift").write_text("class User {}\n", encoding="utf-8")
    (tmp_path / "kt" / "User.kt").write_text("package kt\nclass User\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    sw = tools.check_duplicate("class User", language="swift")
    kt = tools.check_duplicate("class User", language="kotlin")
    assert sw["blocked"] and sw["existing_qualified_name"] == "sw.User.User"
    assert kt["blocked"] and kt["existing_qualified_name"] == "kt.User.User"
