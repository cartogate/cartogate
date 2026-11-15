"""Section 4 — the four deterministic MCP tools over a really-indexed package."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.schema.enums import Confidence, Language, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
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
        "read_symbol",
        "implementations",
        "repo_map",
        "impact_summary",
        "localize",
        "slice",
        "contract_status",
        "gate_history",
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


# --- check_duplicate when the caller names no language ---------------------------------------
# The tool defaults `language` to python, so a TypeScript or Go signature checked without that
# argument searched only the Python table and came back with a silent all-clear.

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _function(qualified_name: str, unit: str, signature: str, language: Language) -> Node:
    return Node.create(
        repo_id="poly",
        qualified_name=qualified_name,
        kind=NodeKind.SYMBOL,
        name=qualified_name.rsplit(".", 1)[-1],
        unit=unit,
        signature=signature,
        location=Location(path=unit, start_line=1, end_line=3),
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=qualified_name,
        language=language,
        is_top_level=True,
    )


def _polyglot_tools() -> CartogateTools:
    """One exported top-level function per language, each alone in its own file."""
    functions = [
        ("web.dates.formatDate", "web/dates.ts", "formatDate(d: Date)", Language.TYPESCRIPT),
        ("svc.Parse", "svc/parse.go", "Parse(data []byte)", Language.GO),
        ("core.load", "core/load.rs", "load(path: &str)", Language.RUST),
        ("app.slug.slugify", "app/slug.py", "def slugify(text):", Language.PYTHON),
    ]
    store = InMemoryStore()
    store.bulk_load(
        (unit, [_function(qname, unit, signature, language)], [])
        for qname, unit, signature, language in functions
    )
    return CartogateTools(store)


@pytest.mark.parametrize(
    ("signature", "language", "existing", "location"),
    [
        ("function formatDate(d: Date)", "typescript", "web.dates.formatDate", "web/dates.ts:1"),
        ("func Parse(data []byte) (*Node, error)", "go", "svc.Parse", "svc/parse.go:1"),
        ("pub const fn load(path: &str) -> Node", "rust", "core.load", "core/load.rs:1"),
    ],
)
def test_a_duplicate_in_another_language_is_reported_not_blocked_when_none_is_named(
    signature: str, language: str, existing: str, location: str
) -> None:
    result = dispatch(_polyglot_tools(), "check_duplicate", {"signature": signature})

    assert result["blocked"] is False and result["language"] == "python"
    assert result["near_match"]["qualified_name"] == existing
    assert result["near_match"]["location"] == location
    assert result["near_match"]["language"] == language
    assert f"language='{language}'" in result["note"]


@pytest.mark.parametrize(
    ("signature", "language", "existing"),
    [
        ("function formatDate(d: Date)", "typescript", "web.dates.formatDate"),
        ("func Parse(data []byte) (*Node, error)", "go", "svc.Parse"),
        ("pub const fn load(path: &str) -> Node", "rust", "core.load"),
        ("def slugify(text):", "python", "app.slug.slugify"),
    ],
)
def test_naming_the_language_blocks_the_duplicate(
    signature: str, language: str, existing: str
) -> None:
    result = dispatch(
        _polyglot_tools(), "check_duplicate", {"signature": signature, "language": language}
    )

    assert result["blocked"] is True and result["language"] == language
    assert result["existing_qualified_name"] == existing and "note" not in result


def test_a_named_language_is_not_second_guessed() -> None:
    result = dispatch(
        _polyglot_tools(),
        "check_duplicate",
        {"signature": "function formatDate(d: Date)", "language": "python"},
    )

    assert result["blocked"] is False and result["language"] == "python"
    assert result["near_match"] is None and "note" not in result


def test_python_stays_the_default_and_still_blocks() -> None:
    result = dispatch(_polyglot_tools(), "check_duplicate", {"signature": "def slugify(text):"})

    assert result["blocked"] is True and result["language"] == "python"
    assert result["existing_qualified_name"] == "app.slug.slugify" and "note" not in result


def test_a_python_duplicate_outranks_a_match_in_another_language() -> None:
    store = InMemoryStore()
    store.bulk_load(
        [
            ("a.py", [_function("a.run", "a.py", "def run():", Language.PYTHON)], []),
            ("b.go", [_function("b.run", "b.go", "run()", Language.GO)], []),
        ]
    )

    result = dispatch(CartogateTools(store), "check_duplicate", {"signature": "def run():"})

    assert result["blocked"] is True and result["existing_qualified_name"] == "a.run"


def test_a_new_signature_is_clear_in_every_language() -> None:
    tools = _polyglot_tools()

    result = dispatch(tools, "check_duplicate", {"signature": "def brand_new(z):"})

    assert result == tools.check_duplicate("def brand_new(z):", language="python")
    assert result["blocked"] is False and result["near_match"] is None and "note" not in result


@pytest.mark.parametrize(
    "fixture",
    ["sample_ts", "sample_go", "sample_rust", "sample_c", "sample_kotlin", "sample_swift"],
)
def test_every_indexed_function_is_found_with_and_without_its_language(fixture: str) -> None:
    """Over really-indexed code, in the signature form the index stores: naming the language
    blocks on the symbol itself, and leaving it out is never a silent all-clear."""
    store = InMemoryStore()
    indexed = index_package(FIXTURES / fixture, repo_id="t", store=store, resolve=False)
    tools = CartogateTools(store)
    functions = [
        n
        for n in indexed.nodes
        if n.kind is NodeKind.SYMBOL and n.is_top_level and n.signature and not n.is_type_decl
    ]
    assert functions, f"{fixture} must define top-level functions"

    for node in functions:
        named = tools.check_duplicate(node.signature, language=node.language.value)
        unnamed = tools.check_duplicate(node.signature)

        assert named["blocked"] is True and named["language"] == node.language.value
        assert unnamed["blocked"] is False and unnamed["language"] == "python"
        assert unnamed["near_match"]["language"] == node.language.value, node.signature


def test_the_check_duplicate_spec_says_what_an_unnamed_language_does() -> None:
    spec = next(s for s in TOOL_SPECS if s["name"] == "check_duplicate")
    language = spec["input_schema"]["properties"]["language"]
    output = spec["output_schema"]["properties"]

    assert language["default"] == "python" and "near_match" in language["description"]
    assert spec["input_schema"]["required"] == ["signature"]
    assert output["language"] == {"type": "string"} and output["note"] == {"type": "string"}
    assert output["near_match"]["properties"]["language"] == {"type": "string"}


@pytest.mark.parametrize(
    "arguments",
    [
        {"signature": "function formatDate(d: Date)"},
        {"signature": "function formatDate(d: Date)", "language": "typescript"},
        {"signature": "def slugify(text):"},
        {"signature": "def brand_new(z):"},
    ],
)
def test_check_duplicate_results_validate_against_the_advertised_schema(
    arguments: dict[str, str],
) -> None:
    import jsonschema

    from cartogate.mcp.server import _with_operational_shapes

    spec = next(s for s in TOOL_SPECS if s["name"] == "check_duplicate")
    result = dispatch(_polyglot_tools(), "check_duplicate", dict(arguments))

    jsonschema.validate(result, spec["output_schema"])
    jsonschema.validate(result, _with_operational_shapes(spec["output_schema"]))


def test_an_unknown_language_is_still_an_error() -> None:
    with pytest.raises(ValueError, match="cobol"):
        _polyglot_tools().check_duplicate("def f():", language="cobol")


# --- findings from the Task 6 review ---------------------------------------------------------


def _same_name_in(*languages: Language) -> CartogateTools:
    """A store holding ``validate(name)`` once per given language, each in its own file."""
    store = InMemoryStore()
    store.bulk_load(
        (
            f"v.{language.value}",
            [
                _function(
                    f"{language.value}.validate", f"v.{language.value}", "validate(name)", language
                )
            ],
            [],
        )
        for language in languages
    )
    return CartogateTools(store)


def test_the_note_names_every_language_that_matched_not_just_the_first() -> None:
    """Go's ``validate(name string)`` and Swift's ``validate(name: String)`` normalise alike, so
    an unnamed check cannot tell which the caller is writing. The note must not pick one."""
    tools = _same_name_in(Language.SWIFT, Language.GO)

    result = tools.check_duplicate("func validate(name: String) -> Bool")

    assert result["blocked"] is False
    assert result["near_match"]["language"] == "go"  # first in the enum's order
    assert "go, swift" in result["note"] and "language='go'" not in result["note"]


def test_a_single_matching_language_is_named_as_the_one_to_pass() -> None:
    result = _same_name_in(Language.SWIFT).check_duplicate("func validate(name: String) -> Bool")

    assert result["near_match"]["language"] == "swift" and "language='swift'" in result["note"]


def test_every_near_match_says_which_language_it_is_in() -> None:
    """A type declaration matched by signature alone is a near match in the checked language."""
    store = InMemoryStore()
    indexed = index_package(FIXTURES / "sample_pkg", repo_id="t", store=store, resolve=False)
    tools = CartogateTools(store)
    classes = [n for n in indexed.nodes if n.kind is NodeKind.SYMBOL and n.is_type_decl]
    assert classes, "the Python fixture must declare a class"

    result = tools.check_duplicate(classes[0].signature, language="python")

    assert result["blocked"] is False
    assert result["near_match"]["qualified_name"] == classes[0].qualified_name
    assert result["near_match"]["language"] == "python"


def test_a_python_near_match_does_not_hide_a_match_in_another_language() -> None:
    store = InMemoryStore()
    indexed = index_package(FIXTURES / "sample_pkg", repo_id="t", store=store, resolve=False)
    python_class = next(n for n in indexed.nodes if n.kind is NodeKind.SYMBOL and n.is_type_decl)
    store.upsert_unit(
        "Model.kt",
        [_function("kt.Model", "Model.kt", python_class.signature, Language.KOTLIN)],
        [],
    )

    result = CartogateTools(store).check_duplicate(python_class.signature)

    assert result["blocked"] is False and result["near_match"]["language"] == "python"
    assert "kotlin" in result["note"]


def test_the_edited_file_is_excluded_from_the_scan_of_other_languages() -> None:
    tools = _same_name_in(Language.GO)

    edited = tools.check_duplicate("validate(name)", exclude_unit="v.go")
    elsewhere = tools.check_duplicate("validate(name)", exclude_unit="other.go")

    assert edited["near_match"] is None and "note" not in edited
    assert elsewhere["near_match"]["language"] == "go"


def test_no_stored_signature_gets_a_silent_all_clear_in_a_polyglot_repository() -> None:
    """All eleven fixture languages indexed together: every function and type, checked in the
    form the index stores it and with no language named, is blocked (Python) or reported."""
    store = InMemoryStore()
    indexed = index_package(FIXTURES, repo_id="fx", store=store, resolve=False)
    tools = CartogateTools(store)
    symbols = [
        n for n in indexed.nodes if n.kind is NodeKind.SYMBOL and n.is_top_level and n.signature
    ]
    assert len({n.language for n in symbols}) >= 10

    for node in symbols:
        result = tools.check_duplicate(node.signature)
        assert result["blocked"] or result["near_match"] is not None, (
            node.language,
            node.signature,
        )
        assert result["blocked"] is False or result["language"] == "python"
