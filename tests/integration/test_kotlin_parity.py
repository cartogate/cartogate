"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on Kotlin."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_kotlin"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_kotlin", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_kotlin_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("models.User")["affected"]}
    assert "service.AuthService.make" in affected  # make() returns/constructs User


def test_find_references_kotlin() -> None:
    report = _tools().find_references("service.AuthService.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "AuthServiceTest.AuthServiceTest.testAuthenticate" in refs  # the test calls authenticate


def test_suggest_tests_kotlin() -> None:
    out = _tools().suggest_tests(symbols=["service.AuthService.authenticate"])
    assert "AuthServiceTest.AuthServiceTest.testAuthenticate" in {
        t["qualified_name"] for t in out["tests"]
    }


def test_doc_drift_kotlin() -> None:
    docs = _tools().doc_drift(symbols=["service.AuthService.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
