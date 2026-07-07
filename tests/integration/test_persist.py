"""F-09 Stage 1: persist a built graph and load it back (the cold-start foundation)."""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance
from cartogate.schema.nodes import Authorship, Location, Node
from cartogate.store import InMemoryStore
from cartogate.store.persist import graph_path, load_graph, save_graph


def _pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    return tmp_path


def _built(tmp_path: Path) -> InMemoryStore:
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def _calls(store: InMemoryStore) -> set[tuple[str, str]]:
    sub = store.subgraph(store.visible_node_ids(), edge_types=(EdgeType.CALLS,))
    by_id = {n.id: n for n in sub.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in sub.edges
        if e.src in by_id and e.dst in by_id
    }


def test_save_load_round_trips_the_graph(tmp_path: Path) -> None:
    _pkg(tmp_path)
    original = _built(tmp_path)
    path = graph_path(tmp_path)
    save_graph(original, path, repo_id="pkg", base=tmp_path)
    assert path.exists()

    loaded = load_graph(path)
    assert loaded is not None
    # Same nodes (by id + qname) and same resolved edges as the freshly-built graph.
    assert loaded.store.visible_node_ids() == original.visible_node_ids()
    orig_qnames = {n.qualified_name for n in original.subgraph(original.visible_node_ids()).nodes}
    loaded_nodes = loaded.store.subgraph(loaded.store.visible_node_ids()).nodes
    assert {n.qualified_name for n in loaded_nodes} == orig_qnames
    assert _calls(loaded.store) == _calls(original)  # cross-file CALLS edge survived the round-trip
    assert loaded.repo_id == "pkg"
    # Full per-node fidelity: a loaded node equals the built one field-for-field (frozen pydantic
    # __eq__ covers enums, nested Location, signature, is_top_level, content_hash, …) — so the gate
    # and tools behave identically on a loaded vs a freshly-built graph.
    helper_orig = original.get_symbol("pkg.a.helper")
    helper_loaded = loaded.store.get_symbol("pkg.a.helper")
    assert helper_orig is not None and helper_loaded == helper_orig


def test_empty_store_round_trips(tmp_path: Path) -> None:
    path = graph_path(tmp_path)
    save_graph(InMemoryStore(), path, repo_id="pkg", base=tmp_path)
    loaded = load_graph(path)
    assert loaded is not None and loaded.store.visible_node_ids() == set()


def test_content_hashes_track_real_files_not_synthetic_units(tmp_path: Path) -> None:
    _pkg(tmp_path)
    save_graph(_built(tmp_path), graph_path(tmp_path), repo_id="pkg", base=tmp_path)
    loaded = load_graph(graph_path(tmp_path))
    assert loaded is not None
    assert loaded.content_hashes["pkg/a.py"] is not None  # a real source file is hashed
    # the <externals> synthetic unit (if present) carries no content hash
    assert all(h is None for u, h in loaded.content_hashes.items() if u.startswith("<"))


def test_constructed_node_round_trips_top_level_and_authorship(tmp_path: Path) -> None:
    # Exercises fields the simple fixture doesn't: is_top_level=True (the gate's fail-closed flag)
    # and Authorship.owners (a tuple that JSON serializes as a list — must coerce back to a tuple).
    node = Node.create(
        repo_id="pkg",
        qualified_name="pkg.x.f",
        kind=NodeKind.SYMBOL,
        name="f",
        unit="pkg/x.py",
        location=Location(path="pkg/x.py", start_line=1, end_line=2),
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash="abc",
        signature="def f():",
        is_top_level=True,
        authorship=Authorship(owners=("alice", "bob")),
    )
    store = InMemoryStore()
    store.bulk_load([("pkg/x.py", [node], [])])
    path = graph_path(tmp_path)
    save_graph(store, path, repo_id="pkg", base=tmp_path)
    loaded = load_graph(path)
    assert loaded is not None
    got = loaded.store.get_node(node.id)
    assert got == node  # is_top_level + Authorship(owners=tuple) survived exactly
    assert got is not None and got.authorship is not None
    assert got.authorship.owners == ("alice", "bob")


def test_save_is_atomic_leaves_no_temp_file(tmp_path: Path) -> None:
    _pkg(tmp_path)
    path = graph_path(tmp_path)
    save_graph(_built(tmp_path), path, repo_id="pkg", base=tmp_path)
    save_graph(_built(tmp_path), path, repo_id="pkg", base=tmp_path)  # overwrite
    assert path.exists()
    leftovers = [p.name for p in path.parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []  # the pid-tagged temp was renamed into place, not left behind


def test_save_error_cleans_up_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pkg(tmp_path)
    path = graph_path(tmp_path)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", _boom)  # fail mid-write, before os.replace
    with pytest.raises(OSError, match="disk full"):
        save_graph(_built(tmp_path), path, repo_id="pkg", base=tmp_path)
    # the finally must remove the pid-tagged temp even though the write failed
    assert [p.name for p in path.parent.iterdir() if ".tmp" in p.name] == []
    assert not path.exists()  # and no partial snapshot was left in place


def test_corrupt_snapshot_logs_a_reason(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = graph_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not gzip")
    with caplog.at_level(logging.INFO, logger="cartogate"):
        assert load_graph(path) is None
    assert any("unreadable" in r.message for r in caplog.records)  # diagnosable, not silent


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_graph(graph_path(tmp_path)) is None


def test_load_corrupt_returns_none(tmp_path: Path) -> None:
    path = graph_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not gzip")
    assert load_graph(path) is None


def test_load_version_mismatch_returns_none(tmp_path: Path) -> None:
    _pkg(tmp_path)
    path = graph_path(tmp_path)
    save_graph(_built(tmp_path), path, repo_id="pkg", base=tmp_path)
    # Tamper the format version -> the snapshot is rejected (caller rebuilds).
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["format_version"] = 999
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    assert load_graph(path) is None


def test_load_id_scheme_version_mismatch_returns_none(tmp_path: Path) -> None:
    # A stale node-id scheme must be rejected (else cross-language qname clashes could collide).
    _pkg(tmp_path)
    path = graph_path(tmp_path)
    save_graph(_built(tmp_path), path, repo_id="pkg", base=tmp_path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["id_scheme_version"] = 1  # stale: language wasn't folded into ids until v2
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    assert load_graph(path) is None
