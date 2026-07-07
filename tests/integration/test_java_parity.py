"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on Java."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_java"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_java", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_java_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("app.models.User")["affected"]}
    assert "app.auth.Auth.makeUser" in affected  # makeUser does `new User(...)`


def test_find_references_java() -> None:
    report = _tools().find_references("app.auth.Auth.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "app.auth.AuthTest.testAuthenticate" in refs  # the JUnit test calls Auth.authenticate


def test_suggest_tests_java() -> None:
    out = _tools().suggest_tests(symbols=["app.auth.Auth.authenticate"])
    assert "app.auth.AuthTest.testAuthenticate" in {t["qualified_name"] for t in out["tests"]}


def test_doc_drift_java() -> None:
    docs = _tools().doc_drift(symbols=["app.auth.Auth.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
