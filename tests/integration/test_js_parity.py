"""The advisory engines (blast_radius / find_references / suggest_tests / doc_drift) on JS."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "sample_js"


def _tools(index_docs: bool = True) -> CartogateTools:
    store = InMemoryStore()
    index_package(
        FIXTURE_ROOT, repo_id="sample_js", store=store, base=FIXTURE_ROOT, index_docs=index_docs
    )
    return CartogateTools(store)


def test_blast_radius_finds_js_dependents() -> None:
    affected = {a["qualified_name"] for a in _tools().blast_radius("models.User")["affected"]}
    assert "auth.authenticate" in affected  # authenticate constructs a User


def test_find_references_js() -> None:
    report = _tools().find_references("auth.authenticate")
    refs = {r["qualified_name"] for r in report["references"]}
    assert "App.App" in refs  # the React component calls authenticate
    assert "service.login" in refs  # the CommonJS module calls authenticate


def test_suggest_tests_js() -> None:
    out = _tools().suggest_tests(symbols=["auth.authenticate"])
    units = {t["unit"] for t in out["tests"]}
    assert any(u.endswith("auth.test.js") for u in units)  # the .test.js file is flagged


def test_doc_drift_js() -> None:
    docs = _tools().doc_drift(symbols=["auth.authenticate"])["docs"]
    assert any(d["path"].endswith("README.md") for d in docs)
