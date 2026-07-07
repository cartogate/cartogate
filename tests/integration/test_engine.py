"""Section 3 integration — the engine over a really-indexed package.

Indexes the fixture, then exercises BLOCK against real extracted symbols and the
diff → changed_set → owning-node path end to end.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.block import BlockEngine, BlockKind
from cartogate.engine.invalidate import Invalidator
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.store.base import FileRegion

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"


def _indexed_store() -> InMemoryStore:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    return store


def test_check_duplicate_on_existing_extracted_symbol() -> None:
    store = _indexed_store()
    engine = BlockEngine(store)
    # authenticate(name) really exists in the fixture -> duplicate is blocked.
    result = engine.check_duplicate("def authenticate(name):")
    assert result.blocked is True
    assert result.kind is BlockKind.DUPLICATE
    assert result.existing_qualified_name == "sample_pkg.auth.authenticate"


def test_check_duplicate_novel_function_passes() -> None:
    store = _indexed_store()
    engine = BlockEngine(store)
    assert engine.check_duplicate("def totally_new_thing(a, b, c):").blocked is False


def test_changed_set_maps_regions_to_symbols() -> None:
    store = _indexed_store()
    auth = store.get_symbol("sample_pkg.auth.authenticate")
    assert auth is not None
    # A region covering authenticate's lines resolves back to its node id.
    regions = [FileRegion(auth.location.path, auth.location.start_line, auth.location.end_line)]
    changed = store.changed_set(regions)
    assert auth.id in changed


def test_invalidate_dirties_the_changed_unit() -> None:
    store = _indexed_store()
    auth = store.get_symbol("sample_pkg.auth.authenticate")
    assert auth is not None
    dirty = Invalidator(store).invalidate([auth.unit])
    assert auth.unit in dirty
