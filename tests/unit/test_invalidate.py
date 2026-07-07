"""Section 3 gate — INVALIDATE mode: changed files → dirty units for re-index.

INVALIDATE is the freshness mechanism: given the files a change touched, mark exactly
the owning units dirty (no more, no less). In v0 a unit is a file, so this is a filter of
changed paths down to the ones the store actually indexes.
"""

from __future__ import annotations

from tests.conftest import MakeSymbol

from cartogate.engine.invalidate import Invalidator
from cartogate.store import InMemoryStore


def test_invalidate_marks_only_known_owning_units(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("pkg/a.py", [make_symbol("pkg.a", unit="pkg/a.py")], [])
    store.upsert_unit("pkg/b.py", [make_symbol("pkg.b", unit="pkg/b.py")], [])
    invalidator = Invalidator(store)

    # a.py is indexed (dirty); c.py is not indexed (ignored); b.py untouched.
    dirty = invalidator.invalidate(["pkg/a.py", "pkg/c.py"])
    assert dirty == {"pkg/a.py"}


def test_invalidate_empty_change_is_noop(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("pkg/a.py", [make_symbol("pkg.a", unit="pkg/a.py")], [])
    assert Invalidator(store).invalidate([]) == set()
