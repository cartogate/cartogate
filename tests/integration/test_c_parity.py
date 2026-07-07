"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on C."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_c"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_c", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_c_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("user.create_user")["affected"]}
    assert "auth.authenticate" in affected  # authenticate calls create_user


def test_find_references_c() -> None:
    report = _tools().find_references("auth.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "test_auth.test_authenticate" in refs  # the test calls authenticate


def test_suggest_tests_c() -> None:
    out = _tools().suggest_tests(symbols=["auth.authenticate"])
    assert "test_auth.test_authenticate" in {t["qualified_name"] for t in out["tests"]}


def test_doc_drift_c() -> None:
    docs = _tools().doc_drift(symbols=["auth.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
