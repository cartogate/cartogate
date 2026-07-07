"""Refresh strategies — keeping the daemon's warm graph fresh.

The Stage 1 source of truth is :class:`GitLazyRefresh`: before answering, it cheaply checks
whether the working tree changed (``git status --porcelain`` for an ignore-aware add/delete/
rename signal, plus file mtimes/sizes to catch edits to already-modified files) and, if so,
refreshes the structural store. It fails *safe* (it can never miss a change the way a filesystem
watcher can) and is cross-platform.

When only a handful of files changed and each owns its module alone, the refresh is
**incremental**: it re-parses just those files and reuses the already-extracted facts of every
unchanged file (``index_package`` is re-run for the changed paths only). For a *resolved* graph the
changed files are re-resolved against the whole repo via a :class:`ResolutionContext` (F-36), so
cross-file edges aren't lost. A change that can't be done soundly in isolation falls back to a full
rebuild: a *shared* module (a Java/Go package, a C ``.h``/``.c`` pair), a deletion, a rename/removal
(an unchanged file's edge into the now-gone id would dangle), or a large (>20-file) batch.

Bounded staleness (deliberate, advisory-only): an incremental refresh re-resolves only the *changed*
files, so a previously-unresolvable call in an *unchanged* file does not gain its edge when the
target is newly added — it's corrected on the next full rebuild. (And ``_scan`` watches only source
files, so a doc-only change isn't detected until its source is touched.)
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from typing import Protocol

from cartogate.extract.languages import LANGUAGES, module_qname
from cartogate.extract.pipeline import build_resolution_context, index_package, iter_source_files
from cartogate.gitio import run_git
from cartogate.instrument import SpanRecorder
from cartogate.schema.enums import NodeKind
from cartogate.store import InMemoryStore
from cartogate.store.persist import LoadedGraph, content_hash_of, graph_path, load_graph, save_graph

_LOG = logging.getLogger("cartogate")

#: A clock returning monotonically increasing seconds (injectable for deterministic tests).
Clock = Callable[[], float]

#: Above this many changed files, a *resolved* incremental refresh isn't cheaper than a full rebuild
#: (each file pays jedi resolution), so fall back to full. Structural incremental stays uncapped.
_MAX_INCREMENTAL_FILES = 20

#: rel POSIX path -> (mtime_ns, size): the per-file fingerprint used to compute the change delta.
_FileState = dict[str, tuple[int, int]]


@dataclass(frozen=True, slots=True)
class RefreshInfo:
    """What the most recent (re)build did — surfaced by ``cartogate doctor`` for visibility."""

    mode: str  # "full" | "incremental"
    reindexed: int  # number of files (re)parsed in this build


class RefreshStrategy(Protocol):
    """Produces a fresh warm store: once at startup, then whenever state changes."""

    def prime(self) -> InMemoryStore:
        """Build the initial warm store and record the current state baseline."""
        ...

    def maybe_refresh(self) -> InMemoryStore | None:
        """Return a fresh store if state changed (and the debounce window has passed), else None."""
        ...


class GitLazyRefresh:
    """Git-backed lazy refresh: refresh the structural store when the working tree changes.

    Refreshes incrementally (re-parse only changed files, reuse the rest) when every touched file
    owns its module alone; otherwise rebuilds the whole store.
    """

    def __init__(
        self,
        root: Path,
        *,
        repo_id: str,
        recorder: SpanRecorder | None = None,
        debounce_s: float = 0.25,
        clock: Clock | None = None,
        resolve: bool = False,
        index_docs: bool = False,
    ) -> None:
        self._root = Path(root).resolve()
        # index_package names units / module qnames relative to this base (root.parent), so the
        # refresher must use the same base for its rel paths to line up with the store's units.
        self._base = self._root.parent
        self._repo_id = repo_id
        self._recorder = recorder
        self._debounce_s = debounce_s
        self._clock = clock or time.monotonic
        # The structural daemon builds a resolution-free signature graph (resolve=False); the MCP
        # server wants the full resolved graph (resolve=True). Incremental re-parse is only sound
        # for the structural facts, so a resolved refresher always does a full rebuild on change.
        self._resolve = resolve
        self._index_docs = index_docs
        self._last_signature: str | None = None
        self._last_check: float | None = None
        self._state: _FileState = {}
        self._qname_by_rel: dict[str, str] = {}  # rel -> module qname (for shared-module detection)
        self._store: InMemoryStore | None = None  # last built store (carried forward incrementally)
        self.last_refresh: RefreshInfo | None = None  # what the last (re)build did (for `doctor`)

    def prime(self) -> InMemoryStore:
        """Build (or load) the initial warm store. For a *resolved* daemon this prefers a persisted
        snapshot (F-09): load it in seconds, re-extract only files changed since (F-36), and persist
        the result — so a cold start (reboot / fresh clone / CI / killed daemon) skips the full
        index. A structural daemon rebuilds cheaply and doesn't persist.
        """
        current_state, current_qmap = self._scan()
        store = self._prime_store(current_state, current_qmap)
        self._state, self._qname_by_rel, self._store = current_state, current_qmap, store
        self._last_signature = self._signature(current_state)
        self._last_check = self._clock()
        return store

    def _prime_store(
        self, current_state: _FileState, current_qmap: dict[str, str]
    ) -> InMemoryStore:
        """Load the persisted snapshot and apply a content-hash delta (F-36); fall through to a full
        build if there's no snapshot, the change is beyond incremental scope, or incremental bails
        on a rename. Transiently sets ``self._store``/``self._qname_by_rel`` so the incremental
        helpers see the loaded store as context; ``prime()`` overwrites all three with the result.
        """
        loaded = self._load_snapshot()
        if loaded is not None:
            reextract, deleted = self._snapshot_delta(loaded, current_state)
            if not reextract and not deleted:
                self.last_refresh = RefreshInfo(mode="snapshot", reindexed=0)
                _LOG.info(
                    "cartogate: loaded snapshot (%d units) — repo unchanged",
                    len(loaded.content_hashes),
                )
                return loaded.store  # repo unchanged since the snapshot -> instant; no rewrite
            # Apply the delta with the F-36 incremental machinery, treating the snapshot as the
            # prior store (its symbols are the context the changed files resolve against).
            # The mutation is what makes _can_incremental / _refresh_incremental read loaded.store.
            self._store, self._qname_by_rel = loaded.store, current_qmap
            if self._can_incremental(reextract, deleted, current_qmap):
                store = self._refresh_incremental(reextract | deleted, reextract)  # None -> bailed
                if store is not None:
                    self.last_refresh = RefreshInfo(mode="snapshot+delta", reindexed=len(reextract))
                    _LOG.info(
                        "cartogate: loaded snapshot + refreshed %d changed file(s)", len(reextract)
                    )
                    self._persist(store)
                    return store
            _LOG.info(
                "cartogate: snapshot delta not incrementally applicable (%d changed, %d deleted)"
                " — full rebuild",
                len(reextract), len(deleted),
            )
        store = self._build_full(current_state)
        self.last_refresh = RefreshInfo(mode="full", reindexed=len(current_state))
        self._persist(store)
        return store

    def _load_snapshot(self) -> LoadedGraph | None:
        """The persisted snapshot for this repo, or ``None``. Resolved-only (the structural graph is
        cheap to rebuild); rejected if its repo_id doesn't match (a stale/foreign snapshot)."""
        if not self._resolve:
            return None
        loaded = load_graph(graph_path(self._root))
        if loaded is None:
            return None  # absent (normal) or rejected — load_graph logs any rejection reason
        if loaded.repo_id != self._repo_id:
            _LOG.info(
                "cartogate: snapshot repo_id %r != %r — rebuilding (stale/foreign snapshot)",
                loaded.repo_id, self._repo_id,
            )
            return None
        return loaded

    def _snapshot_delta(
        self, loaded: LoadedGraph, current_state: _FileState
    ) -> tuple[set[str], set[str]]:
        """(files to re-extract, files deleted) vs the snapshot, by source content hash."""
        reextract = {
            rel
            for rel in current_state  # current source files (rel POSIX paths)
            if loaded.content_hashes.get(rel) != content_hash_of(self._base, rel)
        }
        deleted = {
            rel
            for rel in loaded.content_hashes
            if not rel.startswith("<") and rel not in current_state
        }
        return reextract, deleted

    def _persist(self, store: InMemoryStore) -> None:
        """Write the resolved graph snapshot for the next cold start; a failure is non-fatal.

        On Windows the atomic ``os.replace`` can raise ``PermissionError`` if a reader (the other
        surface) has the snapshot open mid-read — caught here as a warning; the existing complete
        file stays intact, so the worst case is a skipped persist, never a corrupt snapshot.
        """
        if not self._resolve:
            return
        try:
            save_graph(store, graph_path(self._root), repo_id=self._repo_id, base=self._base)
        except OSError as exc:
            _LOG.warning("cartogate: could not persist the graph snapshot (%s)", exc)

    def maybe_refresh(self) -> InMemoryStore | None:
        now = self._clock()
        if self._last_check is not None and (now - self._last_check) < self._debounce_s:
            return None  # within the debounce window — don't even check
        self._last_check = now

        new_state, new_qmap = self._scan()
        signature = self._signature(new_state)
        if signature == self._last_signature:
            return None  # nothing changed
        self._last_signature = signature

        added = new_state.keys() - self._state.keys()
        deleted = self._state.keys() - new_state.keys()
        common = new_state.keys() & self._state.keys()
        modified = {rel for rel in common if new_state[rel] != self._state[rel]}
        reextract = added | modified
        touched = reextract | deleted

        store = None
        if touched and self._can_incremental(reextract, deleted, new_qmap):
            store = self._refresh_incremental(touched, reextract)  # None if it bailed (rename)
            if store is not None:
                self.last_refresh = RefreshInfo(mode="incremental", reindexed=len(reextract))
        if store is None:
            # nothing incrementally actionable (git-only signal / shared module / a resolved rename
            # that would dangle an edge) -> full rebuild, reusing the file list we just scanned.
            store = self._build_full(new_state)
            self.last_refresh = RefreshInfo(mode="full", reindexed=len(new_state))

        self._state, self._qname_by_rel, self._store = new_state, new_qmap, store
        # Keep the on-disk snapshot as fresh as the in-memory graph (review of #125: it used to
        # persist only at prime(), so commit-time snapshot readers saw a graph frozen at daemon
        # START — arbitrarily stale). Best-effort, same guarantees as at prime.
        self._persist(store)
        return store

    # ------------------------------------------------------------------ #
    # Incremental vs full
    # ------------------------------------------------------------------ #

    def _can_incremental(
        self, reextract: Iterable[str], deleted: Iterable[str], new_qmap: dict[str, str]
    ) -> bool:
        """Incremental is sound only when every touched file owns its module alone.

        A file that *shares* a module (a Java/Go package directory, a C ``.h``/``.c`` pair) has
        facts spanning several files, so re-parsing it in isolation would mis-own the shared module
        node — those changes need a full rebuild. For a *resolved* graph (F-36), incremental is
        additionally restricted to no deletions (a deletion dangles incoming cross-file edges) and a
        small batch; a rename/removal within a changed file is caught later in
        :meth:`_refresh_incremental` (it can't be seen without re-parsing).
        """
        if self._store is None:
            return False
        reextract, deleted = set(reextract), set(deleted)
        if self._resolve and (deleted or len(reextract) > _MAX_INCREMENTAL_FILES):
            return False
        new_counts = Counter(new_qmap.values())
        old_counts = Counter(self._qname_by_rel.values())
        if any(new_counts[new_qmap.get(rel, "")] != 1 for rel in reextract):
            return False
        return all(old_counts[self._qname_by_rel.get(rel, "")] == 1 for rel in deleted)

    def _refresh_incremental(self, touched: set[str], reextract: set[str]) -> InMemoryStore | None:
        """Carry every unchanged unit's facts forward and re-parse only the changed files.

        Returns ``None`` to signal "fall back to a full rebuild" — for a resolved graph, when a
        changed file *removed/renamed* a symbol (only visible after re-parsing), since an unchanged
        file's edge into that symbol's now-gone id would silently dangle.
        """
        assert self._store is not None
        new = InMemoryStore(recorder=self._recorder)
        # `touched` (rel paths) are the units to drop; everything else is reused without re-parsing.
        new.bulk_load((u, n, e) for u, n, e in self._store.iter_unit_facts() if u not in touched)
        if reextract:
            # Resolved re-extract: resolve the changed files against the rest of the repo (the maps
            # the store already holds) so cross-file edges aren't lost (F-36). Structural: none.
            context = (
                build_resolution_context(self._store, self._base, exclude_rels=reextract)
                if self._resolve
                else None
            )
            index_package(
                self._root,
                repo_id=self._repo_id,
                store=new,
                resolve=self._resolve,
                index_docs=False,  # doc units are carried forward unchanged
                recorder=self._recorder,
                paths=[self._base / rel for rel in sorted(reextract)],
                context=context,
            )
            if self._resolve and self._lost_a_symbol_id(reextract, new):
                return None  # an incoming edge could dangle; full-rebuild instead
        return new

    def _lost_a_symbol_id(self, reextract: set[str], new_store: InMemoryStore) -> bool:
        """True if any re-extracted file dropped a symbol node *id* it had before.

        Comparing ids — not qnames — is the exact invariant: an id is
        ``blake2b(repo_id, language, qname, kind, stmt_ordinal)`` with **no body input**, so a body
        edit keeps it (incremental) while a rename/removal *or a same-qname identity change* (e.g.
        an ``@overload`` count shift moving a symbol's ``stmt_ordinal``) drops it. A dropped id
        means an unchanged file's edge into it would silently vanish — so we full-rebuild instead.
        """
        assert self._store is not None
        before = self._symbol_ids(self._store, reextract)
        after = self._symbol_ids(new_store, reextract)
        return any(before[rel] - after[rel] for rel in reextract)

    @staticmethod
    def _symbol_ids(store: InMemoryStore, rels: set[str]) -> dict[str, set[str]]:
        """rel path -> its symbol node ids, for the given files (empty set for a file with none)."""
        out: dict[str, set[str]] = {rel: set() for rel in rels}
        for unit, nodes, _edges in store.iter_unit_facts():
            if unit in out:
                out[unit] = {n.id for n in nodes if n.kind is NodeKind.SYMBOL}
        return out

    def _build_full(self, scanned: _FileState | None = None) -> InMemoryStore:
        store = InMemoryStore(recorder=self._recorder)
        # Reuse the file list `_scan` already produced (passed as ``paths``) so a full rebuild does
        # not run ``git ls-files`` a second time. ``paths`` with the *complete* set is sound (no
        # shared-module subset hazard — every file is present). ``None`` -> index re-walks the tree.
        paths = (
            [self._base / rel for rel in scanned] if scanned is not None else None
        )
        index_package(
            self._root,
            repo_id=self._repo_id,
            store=store,
            resolve=self._resolve,  # structural-only for the gate daemon; full graph for MCP
            index_docs=self._index_docs,  # docs only where doc_drift is served (the MCP server)
            recorder=self._recorder,
            paths=paths,
        )
        return store

    # ------------------------------------------------------------------ #
    # Scanning / change detection
    # ------------------------------------------------------------------ #

    def _scan(self) -> tuple[_FileState, dict[str, str]]:
        """Snapshot every indexable file's (mtime, size) and its module qname — no parsing."""
        state: _FileState = {}
        qname_by_rel: dict[str, str] = {}
        for path, language in iter_source_files(self._root):
            try:
                stat = path.stat()
                rel = path.relative_to(self._base).as_posix()
            except (OSError, ValueError):
                continue
            state[rel] = (stat.st_mtime_ns, stat.st_size)
            qname_by_rel[rel] = module_qname(rel, LANGUAGES[language])
        return state, qname_by_rel

    def _signature(self, state: _FileState) -> str:
        """A hash that changes whenever the indexed source changes (git porcelain + mtimes/sizes).

        Combines ``git status --porcelain`` (ignore-aware add/delete/rename/untracked signal; empty
        when not a git repo) with each indexed file's mtime/size, so a content edit to a file
        already shown modified is still caught — the fail-safe change trigger.
        """
        digest = blake2b(digest_size=16)
        digest.update(self._git_porcelain().encode("utf-8"))
        digest.update(b"\x00")
        for rel in sorted(state):
            mtime, size = state[rel]
            digest.update(f"{rel}:{mtime}:{size}\x00".encode())
        return digest.hexdigest()

    def _git_porcelain(self) -> str:
        # run_git: temp-file capture + stdin=DEVNULL + a real timeout, so a wedged ``git status``
        # (the Windows pipe-inheritance hang) can never stall prime()/refresh — it falls back to ""
        # (mtime/size-only change detection) instead of blocking the index forever.
        # utf-8/replace (run_git returns bytes) rather than the old text=True locale decode: the
        # output only feeds a change-hash, so a stable decode is all that matters — identical bytes
        # -> identical signature. (A one-time benign "changed" is possible on the first upgrade if
        # the old locale codec differed from utf-8; harmless, just one extra rebuild.)
        out = run_git(["status", "--porcelain"], cwd=self._root, timeout=10)
        return out.decode("utf-8", "replace") if out is not None else ""
