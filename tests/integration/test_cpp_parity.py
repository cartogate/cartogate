"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on C++."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_cpp"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_cpp", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_cpp_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("user.User")["affected"]}
    assert "test_user.test_isActive" in affected  # the test depends on User


def test_find_references_cpp() -> None:
    report = _tools().find_references("user.User.isActive")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "test_user.test_isActive" in refs  # the test calls isActive (declared receiver)


def test_suggest_tests_cpp() -> None:
    out = _tools().suggest_tests(symbols=["user.User.isActive"])
    assert "test_user.test_isActive" in {t["qualified_name"] for t in out["tests"]}


def test_doc_drift_cpp() -> None:
    docs = _tools().doc_drift(symbols=["user.User.isActive"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
