"""Section 4 — the four deterministic MCP tools over a really-indexed package."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"


def _tools() -> CartogateTools:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    return CartogateTools(store)


def test_tool_specs_are_the_expected_tools() -> None:
    assert {s["name"] for s in TOOL_SPECS} == {
        "check_duplicate",
        "blast_radius",
        "find_symbol",
        "find_references",
        "suggest_tests",
        "doc_drift",
        "find_cycles",
        "find_duplicate_bodies",
        "find_dead_code",
        "impact_summary",
        "localize",
        "slice",
    }


def test_check_duplicate_hit_and_clear() -> None:
    tools = _tools()
    hit = tools.check_duplicate("def authenticate(name):")
    assert hit["blocked"] is True
    assert hit["existing_qualified_name"] == "sample_pkg.auth.authenticate"
    # The agent-facing message shape (STRATEGY.md law 1) rides on the response:
    assert hit["action"].startswith("reuse ")
    assert hit["message"].startswith("BLOCKED:")
    assert "ACTION:" in hit["message"] and "Do NOT retry" in hit["message"]

    clear = tools.check_duplicate("def brand_new(z):")
    assert clear["blocked"] is False


def test_symbol_lookup_accepts_bare_and_dotted_suffix_names() -> None:
    """Agents don't know the repo-root qname prefix — a bare or partially-qualified name must
    resolve when it's unambiguous (this was the 'tools return correct-but-empty results' bug)."""
    tools = _tools()
    # Bare name.
    bare = tools.find_symbol("authenticate")
    assert bare["found"] is True
    assert bare["qualified_name"] == "sample_pkg.auth.authenticate"  # canonical name returned
    # Dotted suffix.
    assert tools.find_symbol("auth.authenticate")["found"] is True
    # The other qname-keyed tools resolve the same way.
    radius = tools.blast_radius("validate")
    assert radius["found"] is True and radius["symbol"] == "sample_pkg.auth.validate"
    assert tools.find_references("User")["found"] is True
    # Exact full names keep working, obviously.
    assert tools.find_symbol("sample_pkg.auth.authenticate")["found"] is True


def test_symbol_lookup_miss_returns_candidates() -> None:
    tools = _tools()
    # Wrong dotted path but a real last segment -> not found, with the real names as candidates.
    res = tools.find_symbol("wrongmodule.authenticate")
    assert res["found"] is False
    assert "sample_pkg.auth.authenticate" in res["candidates"]
    # A name that matches nothing at all -> not found, no candidates.
    nothing = tools.find_symbol("no_such_symbol_anywhere")
    assert nothing["found"] is False and nothing["candidates"] == []


def test_ambiguous_suffix_lists_all_candidates(tmp_path: Path) -> None:
    # Two modules defining the same function name: a bare-name lookup must NOT guess — it reports
    # both candidates so the agent can pick.
    pkg = tmp_path / "twins"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "one.py").write_text("def helper(a):\n    return a\n", encoding="utf-8")
    (pkg / "two.py").write_text("def helper(b):\n    return b\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(pkg, repo_id="twins", store=store)
    tools = CartogateTools(store)

    res = tools.find_symbol("helper")
    assert res["found"] is False
    assert res["candidates"] == ["twins.one.helper", "twins.two.helper"]
    # A qualifying suffix disambiguates.
    assert tools.find_symbol("one.helper")["found"] is True


def test_doc_drift_and_localize_resolve_suffixes_too() -> None:
    tools = _tools()
    # doc_drift symbol lists resolve like the other list-mode tools.
    assert tools.doc_drift(symbols=["authenticate"]) == tools.doc_drift(
        symbols=["sample_pkg.auth.authenticate"]
    )
    drift_missing = tools.doc_drift(symbols=["totally_unknown_fn"])
    assert drift_missing["unresolved_symbols"] == {"totally_unknown_fn": []}
    # localize's test name resolves (bare == full); an unresolvable one passes through cleanly to
    # the engine's own not-found shape (no crash, no resolver error).
    assert tools.localize("authenticate") == tools.localize("sample_pkg.auth.authenticate")
    assert isinstance(tools.localize("no_such_test_anywhere"), dict)


def test_module_name_miss_suggests_the_symbols_inside_it() -> None:
    """Agents ask for a MODULE ("query_kb") when they mean its functions — the miss must offer the
    module's symbols, not an empty candidates list (seen verbatim in a user transcript)."""
    tools = _tools()
    res = tools.find_symbol("auth")  # a module, not a symbol
    assert res["found"] is False
    assert "sample_pkg.auth.authenticate" in res["candidates"]
    assert "sample_pkg.auth.validate" in res["candidates"]


def test_suffix_matching_respects_dotted_boundaries() -> None:
    tools = _tools()
    # A mid-segment fragment must NOT match: "enticate" is a substring of "authenticate" but not a
    # dotted suffix of any qname. Wrong results would be worse than empty ones.
    res = tools.find_symbol("enticate")
    assert res["found"] is False and res["candidates"] == []


def test_blast_radius_and_references_misses_carry_candidates() -> None:
    tools = _tools()
    radius = tools.blast_radius("nope.authenticate")
    assert radius["found"] is False
    assert "sample_pkg.auth.authenticate" in radius["candidates"]
    refs = tools.find_references("nope.authenticate")
    assert refs["found"] is False
    assert "sample_pkg.auth.authenticate" in refs["candidates"]


def test_symbol_lists_resolve_suffixes_too() -> None:
    tools = _tools()
    # suggest_tests / impact_summary take symbol LISTS — bare names must resolve there as well.
    via_bare = tools.suggest_tests(symbols=["authenticate"])
    via_full = tools.suggest_tests(symbols=["sample_pkg.auth.authenticate"])
    assert via_bare["tests"] == via_full["tests"]
    impact_bare = tools.impact_summary(symbols=["authenticate"])
    impact_full = tools.impact_summary(symbols=["sample_pkg.auth.authenticate"])
    assert impact_bare == impact_full
    # Unresolvable entries are reported, not silently dropped.
    missing = tools.suggest_tests(symbols=["totally_unknown_fn"])
    assert missing["unresolved_symbols"] == {"totally_unknown_fn": []}


def test_blast_radius_lists_dependents() -> None:
    tools = _tools()
    # authenticate calls validate, so validate's blast radius includes authenticate.
    result = tools.blast_radius("sample_pkg.auth.validate", depth=1)
    assert result["found"] is True
    affected = {a["qualified_name"] for a in result["affected"]}
    assert "sample_pkg.auth.authenticate" in affected


def test_blast_radius_unknown_symbol_is_not_found() -> None:
    result = _tools().blast_radius("sample_pkg.does_not_exist")
    assert result["found"] is False
    assert result["count"] == 0


def test_find_symbol() -> None:
    tools = _tools()
    assert tools.find_symbol("sample_pkg.auth.validate")["found"] is True
    assert tools.find_symbol("sample_pkg.missing")["found"] is False


def test_find_references() -> None:
    tools = _tools()
    result = tools.find_references("sample_pkg.models.User")
    refs = {a["qualified_name"] for a in result["references"]}
    # make_user calls User(); DEFAULT = User references it.
    assert "sample_pkg.auth.make_user" in refs


def test_dispatch_routes_and_rejects_unknown() -> None:
    tools = _tools()
    out = dispatch(tools, "check_duplicate", {"signature": "def authenticate(name):"})
    assert out["blocked"] is True
    with pytest.raises(ValueError, match="unknown tool"):
        dispatch(tools, "bogus_tool", {})


def test_dispatch_routes_localize() -> None:
    # Smoke: the localize route resolves and returns a well-formed report (no diff -> no suspects).
    out = dispatch(_tools(), "localize", {"test": "sample_pkg.auth.authenticate"})
    assert out["found"] is True and out["count"] == 0 and "depth_searched" in out
