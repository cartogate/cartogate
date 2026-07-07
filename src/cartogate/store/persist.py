"""Persist a built graph to disk and load it back (F-09).

The in-memory store rebuilds the whole graph from source on every cold start (a reboot, a fresh
clone, a CI run, a killed daemon). Persisting the EXTRACTED facts lets a cold start *load* the graph
(seconds) instead of re-indexing (minutes), then refresh only what changed since (F-36).

Format: a gzipped JSON snapshot of the store's visible units — each unit's nodes + edges
(``model_dump``/``model_validate`` round-trip the frozen pydantic models) plus a content hash of its
source file for staleness detection. The snapshot is stamped with a format version and the node
``ID_SCHEME_VERSION``; a mismatch on load returns ``None`` (the caller rebuilds), so a schema change
can never silently load stale-shaped ids. Only structural facts are stored — no source, no secrets,
and (being JSON + pydantic ``model_validate``) **no code-execution path** on load.

**Trust model.** ``.cartogate/`` is gitignored, so the snapshot is local by default. Committing it
to share a first build is opt-in and means trusting it like source — a crafted snapshot drives gate
decisions (it can flip ``is_top_level`` / ``signature``). The trust boundary is repo write access,
the same as for source; verify (or don't commit) accordingly.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path

from pydantic import ValidationError

from cartogate.schema.edges import Edge
from cartogate.schema.ids import ID_SCHEME_VERSION
from cartogate.schema.nodes import Node
from cartogate.store import InMemoryStore
from cartogate.store.base import StoreInterface

_LOG = logging.getLogger("cartogate")

#: Bump on any change to the snapshot layout — INCLUDING adding a ``Node``/``Edge`` field that can
#: take a non-default value in a fresh build, so an old snapshot that lacks the key can't silently
#: load with the wrong default. Id-scheme changes also bump ``ID_SCHEME_VERSION``; either mismatch
#: -> None -> rebuild.
_FORMAT_VERSION = 1

#: Persisted-graph file, under the repo's gitignored state dir (next to daemon.json / mcp.log).
_GRAPH_NAME = "graph.json.gz"

#: Digest size (bytes) for per-unit source content hashes. Stage 2's staleness re-hash MUST use the
#: same value, or stored vs recomputed hashes will never compare equal.
_CONTENT_HASH_DIGEST_SIZE = 16


def graph_path(repo: Path) -> Path:
    """Path to the persisted-graph snapshot for ``repo``."""
    return repo / ".cartogate" / _GRAPH_NAME


@dataclass(frozen=True, slots=True)
class LoadedGraph:
    """A graph loaded from a snapshot, with the per-unit source hashes for staleness checks."""

    store: InMemoryStore
    #: Absolute import-root path string, at persist time — machine-specific. Stage 2 staleness
    #: re-hashing must re-derive the current base from the repo root passed on startup, and must NOT
    #: use this value for file I/O (it's wrong on another machine / a moved repo).
    base: str
    repo_id: str
    #: rel POSIX unit -> blake2b hex of its source bytes at persist time (``None`` for synthetic
    #: units like ``<externals>`` / ``<doc:…>``, and for a file that couldn't be read).
    content_hashes: dict[str, str | None]


def _unit_content_hash(unit: str, base: Path) -> str | None:
    """blake2b (``_CONTENT_HASH_DIGEST_SIZE``) of the unit's source file, or ``None`` for a
    synthetic unit / unreadable file. Stage 2's staleness re-hash must use the same digest size."""
    if unit.startswith("<"):  # synthetic units (<externals>, <doc:…>) have no source file
        return None
    try:
        return blake2b(
            (base / unit).read_bytes(), digest_size=_CONTENT_HASH_DIGEST_SIZE
        ).hexdigest()
    except OSError:
        return None


def content_hash_of(base: Path, unit: str) -> str | None:
    """The *current* source-content hash for a unit, computed exactly as :func:`save_graph` stores
    — so a daemon can compare it against a snapshot's stored hash to find what changed (F-09 Stage 3
    staleness). ``None`` for a synthetic unit / unreadable file."""
    return _unit_content_hash(unit, base)


def save_graph(store: StoreInterface, path: Path, *, repo_id: str, base: Path) -> None:
    """Write the store's visible graph to ``path`` (gzipped JSON), stamped for safe reload."""
    units = [
        {
            "unit": unit,
            "content_hash": _unit_content_hash(unit, base),
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
        }
        for unit, nodes, edges in store.iter_unit_facts()
    ]
    payload = {
        "format_version": _FORMAT_VERSION,
        "id_scheme_version": ID_SCHEME_VERSION,
        "repo_id": repo_id,
        "base": str(base),
        "units": units,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name == ".cartogate":  # keep the state dir self-ignoring (see ensure_state_dir)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            with contextlib.suppress(OSError):
                ignore.write_text("*\n", encoding="utf-8")
    # Atomic write: a concurrent reader — or the MCP and its spawned daemon both persisting — must
    # never see a half-written .gz (it would fail to load and force a needless rebuild). Write a
    # pid-tagged temp in the same dir, then os.replace (atomic on Windows + POSIX).
    # (A hard kill between here and the replace can orphan a ``.tmp<pid>`` file — rare, tiny, and
    # gitignored; it can't be mistaken for the snapshot and won't collide with another pid. We do
    # NOT sweep stray temps: an aggressive sweep would race a concurrent writer's in-flight temp.)
    tmp = path.parent / f"{path.name}.tmp{os.getpid()}"
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace; cleans up on error


def load_graph(path: Path) -> LoadedGraph | None:
    """Load a snapshot from ``path``, or ``None`` if it's missing, unreadable, or version-mismatched
    (in which case the caller rebuilds — never loads a stale-shaped graph)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return None  # no snapshot yet — a normal cold start; the caller logs the full build
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        _LOG.info("cartogate: snapshot at %s is unreadable (%s) — rebuilding", path, exc)
        return None
    if not isinstance(payload, dict):
        _LOG.info("cartogate: snapshot at %s is malformed (not an object) — rebuilding", path)
        return None
    if payload.get("format_version") != _FORMAT_VERSION:
        _LOG.info("cartogate: snapshot at %s format version changed — rebuilding", path)
        return None
    if payload.get("id_scheme_version") != ID_SCHEME_VERSION:
        _LOG.info("cartogate: snapshot at %s node-id scheme changed — rebuilding", path)
        return None

    try:
        triples = []
        content_hashes: dict[str, str | None] = {}
        for unit_payload in payload["units"]:
            unit = unit_payload["unit"]
            nodes = [Node.model_validate(n) for n in unit_payload["nodes"]]
            edges = [Edge.model_validate(e) for e in unit_payload["edges"]]
            triples.append((unit, nodes, edges))
            content_hashes[unit] = unit_payload.get("content_hash")
        store = InMemoryStore()
        store.bulk_load(triples)
        return LoadedGraph(
            store=store,
            repo_id=str(payload["repo_id"]),
            base=str(payload["base"]),
            content_hashes=content_hashes,
        )
    except (KeyError, TypeError, ValidationError) as exc:
        _LOG.info("cartogate: snapshot at %s is malformed (%s) — rebuilding", path, exc)
        return None
