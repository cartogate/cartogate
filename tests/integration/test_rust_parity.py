"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on Rust."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_rust"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_rust", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_rust_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("crate.models.User")["affected"]}
    assert "crate.models.User.new" in affected  # `new` returns User


def test_find_references_rust() -> None:
    report = _tools().find_references("crate.auth.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "crate.tests.auth_test.test_authenticate" in refs  # the tests/ file calls it


def test_suggest_tests_rust() -> None:
    out = _tools().suggest_tests(symbols=["crate.auth.authenticate"])
    assert "crate.tests.auth_test.test_authenticate" in {t["qualified_name"] for t in out["tests"]}


def test_doc_drift_rust() -> None:
    docs = _tools().doc_drift(symbols=["crate.auth.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
