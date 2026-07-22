"""Block messages must convert a BLOCK into self-correction, not a retry loop.

Evidence (design law 1): the shape that works is
BLOCKED (what) -> EVIDENCE (the extracted fact, file:line) -> ACTION (the one sanctioned next
step) -> anti-loop ("do NOT retry identical / do not rename to evade"). Measured effect:
identical-retry loops ~60% -> ~10% on message shape alone.
"""

from __future__ import annotations

from tests.conftest import MakeSymbol

from cartogate.engine.block import BlockEngine
from cartogate.store import InMemoryStore


def _dup_result(make_symbol: MakeSymbol):  # type: ignore[no-untyped-def]
    store = InMemoryStore()
    sym = make_symbol("pkg.auth.login", signature="def login(user)", unit="pkg/auth.py")
    store.upsert_unit("pkg/auth.py", [sym], [])
    return BlockEngine(store).check_duplicate("def login(user)")


def test_duplicate_message_carries_the_full_shape(make_symbol: MakeSymbol) -> None:
    message = _dup_result(make_symbol).agent_message()
    assert message.startswith("BLOCKED:")
    assert "EVIDENCE" in message and "EXTRACTED" in message
    assert "pkg.auth.login" in message  # the existing symbol, by name...
    assert ".py:" in message  # ...and its file:line
    assert "ACTION:" in message and "reuse" in message.lower()
    assert "Do NOT retry" in message
    assert "rename" in message  # evasion is named explicitly


def test_contract_message_carries_the_full_shape(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    sym = make_symbol("pkg.auth.login", signature="def login(user)", unit="pkg/auth.py")
    store.upsert_unit("pkg/auth.py", [sym], [])
    result = BlockEngine(store).check_contract(
        "pkg.auth.login", new_signature="def login(user, tenant)"
    )
    assert result.blocked
    assert result.existing_location is not None  # contract breaches now carry the location too
    message = result.agent_message()
    assert message.startswith("BLOCKED:")
    assert "EVIDENCE" in message
    assert "pkg.auth.login" in message
    assert "ACTION:" in message and "find_references" in message
    assert "Do NOT retry" in message


def test_ok_result_has_no_agent_message(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    engine = BlockEngine(store)
    assert engine.check_duplicate("def novel()").agent_message() == ""


def test_message_is_deterministic(make_symbol: MakeSymbol) -> None:
    assert _dup_result(make_symbol).agent_message() == _dup_result(make_symbol).agent_message()
