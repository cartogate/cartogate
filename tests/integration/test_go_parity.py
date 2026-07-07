"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on Go."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_go"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_go", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_go_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("models.User")["affected"]}
    assert "models.NewUser" in affected  # NewUser returns *User


def test_find_references_go() -> None:
    report = _tools().find_references("auth.Authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "auth.TestAuthenticate" in refs  # the _test.go calls Authenticate (same package)


def test_suggest_tests_go() -> None:
    out = _tools().suggest_tests(symbols=["auth.Authenticate"])
    assert "auth.TestAuthenticate" in {t["qualified_name"] for t in out["tests"]}


def test_doc_drift_go() -> None:
    docs = _tools().doc_drift(symbols=["auth.Authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
