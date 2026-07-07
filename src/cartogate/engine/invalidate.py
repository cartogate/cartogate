"""INVALIDATE mode — mark dirty units for re-index (spec §1, §8.2).

The freshness mechanism: given the files a change touched, return exactly the indexed
units that own them. In v0 a unit is a file, so this filters the changed paths down to
the ones the store actually knows about. Re-indexing the dirty units is a separate,
batch step in v0 (a real-time daemon is future work, F-19).
"""

from __future__ import annotations

from collections.abc import Iterable

from cartogate.store.base import StoreInterface


class Invalidator:
    """Resolves changed file paths to the dirty units that must be re-indexed."""

    def __init__(self, store: StoreInterface) -> None:
        self._store = store

    def invalidate(self, changed_paths: Iterable[str]) -> set[str]:
        """Return the indexed units owning any of ``changed_paths`` (unknown paths ignored)."""
        known = self._store.units()
        return {path for path in changed_paths if path in known}
