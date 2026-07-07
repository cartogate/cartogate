"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on C#."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_csharp"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_csharp", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_csharp_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("Models.User")["affected"]}
    assert "Services.AuthService.Make" in affected  # Make does `new User(...)`


def test_find_references_csharp() -> None:
    report = _tools().find_references("Services.AuthService.Authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "AuthServiceTests.AuthServiceTests.TestAuthenticate" in refs  # the test calls it


def test_suggest_tests_csharp() -> None:
    out = _tools().suggest_tests(symbols=["Services.AuthService.Authenticate"])
    assert "AuthServiceTests.AuthServiceTests.TestAuthenticate" in {
        t["qualified_name"] for t in out["tests"]
    }


def test_doc_drift_csharp() -> None:
    docs = _tools().doc_drift(symbols=["Services.AuthService.Authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
