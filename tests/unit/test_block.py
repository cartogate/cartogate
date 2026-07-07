"""Section 3 gate — BLOCK mode: duplicate detection + contract checks.

BLOCK is the only hard-enforcement mode in v0. It fires on two deterministic conditions:
a new symbol whose normalized signature already exists (duplicate), or a change to an
existing symbol's signature/visibility (contract break). It must never fire on anything
but EXTRACTED structural facts.
"""

from __future__ import annotations

from tests.conftest import MakeSymbol

from cartogate.engine.block import BlockEngine, BlockKind
from cartogate.schema.enums import Confidence, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore


def test_duplicate_signature_is_blocked(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    foo = make_symbol("pkg.foo", signature="def foo(x, y):", unit="pkg/a.py")
    store.upsert_unit("pkg/a.py", [foo], [])
    engine = BlockEngine(store)

    result = engine.check_duplicate("def foo(x, y):")
    assert result.blocked is True
    assert result.kind is BlockKind.DUPLICATE
    assert result.existing_symbol_id == foo.id
    assert result.existing_qualified_name == "pkg.foo"
    # Actionable output (F-66): point at where to find/reuse the existing symbol.
    assert result.existing_location == f"{foo.location.path}:{foo.location.start_line}"
    assert result.existing_signature == "def foo(x, y):"
    # the full path:line is surfaced in the human-readable reason
    assert f"{foo.location.path}:{foo.location.start_line}" in result.reason


def test_novel_signature_passes(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("pkg/a.py", [make_symbol("pkg.foo", signature="def foo(x):")], [])
    engine = BlockEngine(store)

    result = engine.check_duplicate("def bar(z):")
    assert result.blocked is False
    assert result.kind is BlockKind.OK


def test_contract_signature_change_is_blocked(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    login = make_symbol("pkg.User.login", signature="def login(self):", unit="pkg/u.py")
    store.upsert_unit("pkg/u.py", [login], [])
    engine = BlockEngine(store)

    result = engine.check_contract("pkg.User.login", new_signature="def login(self, token):")
    assert result.blocked is True
    assert result.kind is BlockKind.CONTRACT


def test_contract_visibility_reduction_is_blocked(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    api = make_symbol("pkg.api", signature="def api():", visibility=Visibility.EXPORTED)
    store.upsert_unit("pkg/a.py", [api], [])
    engine = BlockEngine(store)

    # exported -> internal narrows the public surface: a contract break.
    result = engine.check_contract("pkg.api", new_visibility=Visibility.INTERNAL)
    assert result.blocked is True
    assert result.kind is BlockKind.CONTRACT


def test_contract_unchanged_passes(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    login = make_symbol("pkg.User.login", signature="def login(self):", unit="pkg/u.py")
    store.upsert_unit("pkg/u.py", [login], [])
    engine = BlockEngine(store)

    # Same signature (different spelling) and same visibility — no break.
    result = engine.check_contract("pkg.User.login", new_signature="login(self)")
    assert result.blocked is False
    assert result.kind is BlockKind.OK


def test_contract_on_new_symbol_passes(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    engine = BlockEngine(store)
    # No existing symbol -> nothing to break.
    result = engine.check_contract("pkg.brand_new", new_signature="def brand_new():")
    assert result.blocked is False
    assert result.kind is BlockKind.OK


def test_contract_skips_when_existing_has_no_signature(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("m.py", [make_symbol("pkg.thing", signature=None)], [])
    engine = BlockEngine(store)
    # No established signature contract -> adding one is not a break.
    assert engine.check_contract("pkg.thing", new_signature="def thing(x):").blocked is False


def test_inferred_node_with_signature_is_not_a_duplicate() -> None:
    # Risk R7, code-enforced: an INFERRED fact must never drive a BLOCK, even if it carries
    # a signature that matches the query. The store's signature index excludes it.
    store = InMemoryStore()
    ghost = Node.create(
        repo_id="t",
        qualified_name="pkg.ghost",
        kind=NodeKind.SYMBOL,
        name="ghost",
        unit="pkg/a.py",
        signature="def ghost(x):",
        location=Location(path="pkg/a.py", start_line=1, end_line=1),
        provenance=Provenance.SEMANTIC_SKILL,
        confidence=Confidence.INFERRED,
        content_hash="g",
    )
    store.upsert_unit("pkg/a.py", [ghost], [])
    assert BlockEngine(store).check_duplicate("def ghost(x):").blocked is False
