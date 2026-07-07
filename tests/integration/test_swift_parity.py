"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on Swift."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_swift"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_swift", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_swift_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("Models.User")["affected"]}
    assert "Service.AuthService.make" in affected  # make() constructs User


def test_find_references_swift() -> None:
    report = _tools().find_references("Service.AuthService.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "AuthServiceTests.AuthServiceTests.testAuthenticate" in refs  # the test calls it


def test_suggest_tests_swift() -> None:
    out = _tools().suggest_tests(symbols=["Service.AuthService.authenticate"])
    assert "AuthServiceTests.AuthServiceTests.testAuthenticate" in {
        t["qualified_name"] for t in out["tests"]
    }


def test_doc_drift_swift() -> None:
    docs = _tools().doc_drift(symbols=["Service.AuthService.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
