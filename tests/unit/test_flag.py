"""Unit tests for FLAG mode (test-drift): classification + the FlagEngine over a built store."""

from __future__ import annotations

from tests.conftest import MakeSymbol

from cartogate.engine.flag import FlagEngine, is_test_unit
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.store import InMemoryStore


def _edge(src: str, dst: str, edge_type: EdgeType) -> Edge:
    return Edge(
        type=edge_type,
        src=src,
        dst=dst,
        provenance=Provenance.LSP,
        confidence=Confidence.EXTRACTED,
    )


def test_is_test_unit() -> None:
    assert is_test_unit("pkg/tests/test_auth.py") is True
    assert is_test_unit("tests/test_auth.py") is True
    assert is_test_unit("pkg/test/helper.py") is True  # singular `test/` dir counts
    assert is_test_unit("pkg/auth_test.py") is True
    assert is_test_unit("pkg/conftest.py") is True
    assert is_test_unit("pkg/auth.py") is False
    assert is_test_unit("pkg/contestant.py") is False  # not a test despite 'test' substring
    assert is_test_unit("pkg/latest.py") is False
    assert is_test_unit("test.py") is False  # bare test.py (no test_ prefix) is not a test


def test_tests_for_symbols_flags_only_test_callers(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    target = make_symbol(
        "pkg.auth.authenticate", signature="def authenticate(n):", unit="pkg/auth.py"
    )
    a_test = make_symbol("pkg.tests.test_auth.test_login", unit="pkg/tests/test_auth.py")
    a_caller = make_symbol("pkg.api.handler", unit="pkg/api.py")  # a non-test caller
    edges = [
        _edge(a_test.id, target.id, EdgeType.CALLS),
        _edge(a_caller.id, target.id, EdgeType.CALLS),
    ]
    store.upsert_unit("pkg/auth.py", [target], [])
    store.upsert_unit("pkg/tests/test_auth.py", [a_test], [])
    store.upsert_unit("pkg/api.py", [a_caller], edges)

    report = FlagEngine(store).tests_for_symbols(["pkg.auth.authenticate"])
    payload = report.to_dict()
    assert payload["count"] == 1
    assert payload["tests"][0]["qualified_name"] == "pkg.tests.test_auth.test_login"
    assert payload["tests"][0]["exercises"] == ["pkg.auth.authenticate"]
    assert payload["changed"] == ["pkg.auth.authenticate"]


def test_tests_for_symbols_counts_references_too(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    target = make_symbol("pkg.m.User", signature="class User:", unit="pkg/m.py")
    a_test = make_symbol("pkg.tests.test_m.test_user", unit="pkg/tests/test_m.py")
    store.upsert_unit("pkg/m.py", [target], [])
    store.upsert_unit(
        "pkg/tests/test_m.py", [a_test], [_edge(a_test.id, target.id, EdgeType.REFERENCES)]
    )
    report = FlagEngine(store).tests_for_symbols(["pkg.m.User"])
    assert report.to_dict()["count"] == 1


def test_symbol_with_no_tests_returns_empty(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("pkg/a.py", [make_symbol("pkg.a.foo", unit="pkg/a.py")], [])
    report = FlagEngine(store).tests_for_symbols(["pkg.a.foo"])
    assert report.to_dict()["count"] == 0


def test_unknown_symbol_is_ignored(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    report = FlagEngine(store).tests_for_symbols(["pkg.nope"])
    assert report.to_dict() == {"changed": [], "tests": [], "count": 0}


def test_empty_inputs_return_empty_reports(make_symbol: MakeSymbol) -> None:
    engine = FlagEngine(InMemoryStore())
    assert engine.tests_for_symbols([]).to_dict()["count"] == 0
    assert engine.tests_for_diff("").to_dict()["count"] == 0  # empty diff -> no changes
