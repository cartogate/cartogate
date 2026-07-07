"""End-to-end Kotlin extraction over the sample_kotlin fixture (nodes + resolved edges + gate)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_kotlin"


def _index(index_docs: bool = False) -> tuple[InMemoryStore, object]:
    store = InMemoryStore()
    # base = the fixture root: each .kt file is its own module; the resolver reads the in-file
    # `package`/`import` to bind cross-package references.
    result = index_package(
        FIXTURE_ROOT, repo_id="sample_kotlin", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return store, result


def _edges(result) -> set[tuple[str, str, str]]:
    qn = {n.id: n.qualified_name for n in result.nodes}
    return {(qn.get(e.src, "?"), e.type.value, qn.get(e.dst, "?")) for e in result.edges}


def test_symbol_and_module_nodes() -> None:
    _, result = _index()
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert {"models.Base", "models.User", "models.User.isActive",
            "service.AuthService", "service.validate"} <= symbols
    modules = {n.qualified_name for n in result.nodes if n.kind is NodeKind.MODULE}
    assert {"models", "service", "AuthServiceTest"} <= modules


def test_defines_edges() -> None:
    edges = _edges(_index()[1])
    assert ("models", "defines", "models.User") in edges  # module -> class
    assert ("service", "defines", "service.validate") in edges  # module -> top-level function
    assert ("service.AuthService", "defines", "service.AuthService.authenticate") in edges


def test_resolved_edges() -> None:
    edges = _edges(_index()[1])
    assert ("models.User", "inherits", "models.Base") in edges
    assert ("service.AuthService.authenticate", "calls",
            "models.User.isActive") in edges  # declared-receiver `u: User`
    assert ("service.AuthService.authenticate", "calls",
            "service.validate") in edges  # top-level function call
    assert ("service.AuthService.make", "calls", "models.User") in edges  # User(...) constructor
    assert ("service", "imports", "models.User") in edges


def test_nodes_tagged_kotlin() -> None:
    _, result = _index()
    syms = [n for n in result.nodes if n.kind is NodeKind.SYMBOL]
    assert syms and all(n.language is Language.KOTLIN for n in syms)


def test_kotlin_duplicate_gate_blocks_existing_type() -> None:
    store, _ = _index()
    tools = CartogateTools(store)
    hit = dispatch(tools, "check_duplicate", {"signature": "class User", "language": "kotlin"})
    assert hit["blocked"] is True and hit["existing_qualified_name"] == "models.User"
    assert dispatch(tools, "check_duplicate",
                    {"signature": "class Brandnew", "language": "kotlin"})["blocked"] is False


def test_kotlin_and_java_do_not_cross_collide(tmp_path: Path) -> None:
    (tmp_path / "kt").mkdir()
    (tmp_path / "jv").mkdir()
    (tmp_path / "kt" / "User.kt").write_text("package kt\nclass User\n", encoding="utf-8")
    (tmp_path / "jv" / "User.java").write_text(
        "package jv;\npublic class User {}\n", encoding="utf-8"
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="mix", store=store, base=tmp_path, index_docs=False)
    tools = CartogateTools(store)
    kt = tools.check_duplicate("class User", language="kotlin")
    jv = tools.check_duplicate("class User {}", language="java")
    assert kt["blocked"] and kt["existing_qualified_name"] == "kt.User.User"
    assert jv["blocked"] and jv["existing_qualified_name"] == "jv.User"
