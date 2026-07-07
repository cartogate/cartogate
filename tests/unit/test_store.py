"""Gate tests for the store interface + in-memory Glean-style unit stacking (Section 1).

Proves the §8.1 contract and the immutable-stacking semantics: an incremental update
*hides* a unit's prior facts (retained, but invisible to queries) and stacks the new
facts on top — never an in-place mutation. Also proves typed-edge filtering and that
the store mechanically honors an EXTRACTED-only confidence filter (the engine supplies
that filter as the single gate-policy chokepoint in Section 3).
"""

from __future__ import annotations

import pytest

from cartogate.instrument import Phase, SpanRecorder
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import Direction, InMemoryStore

REPO = "repoA"


def make_symbol(
    qualified_name: str,
    *,
    signature: str | None,
    unit: str = "m.py",
    content: str = "x",
    is_top_level: bool = True,
) -> Node:
    name = qualified_name.rsplit(".", 1)[-1]
    return Node.create(
        repo_id=REPO,
        qualified_name=qualified_name,
        kind=NodeKind.SYMBOL,
        name=name,
        unit=unit,
        signature=signature,
        location=Location(path=unit, start_line=1, end_line=2),
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=content,
        is_top_level=is_top_level,
    )


def make_edge(
    src: Node,
    dst: Node,
    edge_type: EdgeType,
    confidence: Confidence = Confidence.EXTRACTED,
) -> Edge:
    provenance = (
        Provenance.TREE_SITTER if confidence is Confidence.EXTRACTED else Provenance.SEMANTIC_SKILL
    )
    return Edge(
        type=edge_type,
        src=src.id,
        dst=dst.id,
        provenance=provenance,
        confidence=confidence,
    )


# --------------------------------------------------------------------------- #
# exists() / get_symbol()
# --------------------------------------------------------------------------- #


def test_exists_and_get_symbol() -> None:
    store = InMemoryStore()
    foo = make_symbol("pkg.foo", signature="def foo(x, y):", unit="pkg/a.py")
    store.upsert_unit("pkg/a.py", [foo], [])

    assert store.exists("foo(x, y)") is True  # normalized internally
    assert store.exists("foo(x)") is False
    assert store.get_symbol("pkg.foo") is not None
    assert store.get_symbol("pkg.missing") is None


def test_methods_excluded_from_duplicate_index() -> None:
    # The signature index feeds the duplicate gate, which applies to top-level
    # functions/classes only — methods are reachable by qualified name but not by
    # signature, so two classes' same-named methods never collide as "duplicates".
    store = InMemoryStore()
    method = make_symbol("pkg.C.method", signature="def method(self, x):", is_top_level=False)
    free = make_symbol("pkg.free_fn", signature="def free_fn(x):", is_top_level=True)
    store.upsert_unit("c.py", [method, free], [])
    # Method: reachable by name, but excluded from the duplicate index.
    assert store.exists("method(x)") is False
    assert store.find_symbols_by_signature("method(x)") == []
    assert store.get_symbol("pkg.C.method") is not None
    # Co-located positive: the free function IS in the duplicate index.
    assert store.exists("free_fn(x)") is True


# --------------------------------------------------------------------------- #
# Immutable unit stacking: upsert / hide / replace
# --------------------------------------------------------------------------- #


def test_replace_unit_hides_removed_node_but_retains_it() -> None:
    store = InMemoryStore()
    foo = make_symbol("pkg.foo", signature="def foo(x):", unit="pkg/a.py")
    bar = make_symbol("pkg.bar", signature="def bar():", unit="pkg/a.py")
    qux = make_symbol("pkg.qux", signature="def qux():", unit="pkg/b.py")
    store.upsert_unit("pkg/a.py", [foo, bar], [])
    store.upsert_unit("pkg/b.py", [qux], [])

    # Re-index pkg/a.py: bar removed, baz added, foo kept.
    baz = make_symbol("pkg.baz", signature="def baz():", unit="pkg/a.py")
    store.replace_unit("pkg/a.py", [foo, baz], [])

    # Visible: foo, baz, qux. Never the removed bar.
    assert store.get_symbol("pkg.foo") is not None
    assert store.get_symbol("pkg.baz") is not None
    assert store.get_symbol("pkg.qux") is not None  # other unit untouched
    assert store.get_symbol("pkg.bar") is None  # invisible

    # bar's fact is retained in the hidden history, just not visible.
    assert bar.id in store.hidden_node_ids()
    assert bar.id not in store.visible_node_ids()


def test_upsert_same_unit_overwrites_and_hides_prior() -> None:
    store = InMemoryStore()
    v1 = make_symbol("pkg.foo", signature="def foo(x):", unit="pkg/a.py", content="v1")
    store.upsert_unit("pkg/a.py", [v1], [])
    v2 = make_symbol("pkg.foo", signature="def foo(x, y):", unit="pkg/a.py", content="v2")
    store.upsert_unit("pkg/a.py", [v2], [])

    # Same qualified_name => same id; the visible fact is the new content/signature.
    assert v1.id == v2.id
    assert store.exists("foo(x, y)") is True
    assert store.exists("foo(x)") is False  # old signature no longer visible
    visible = store.get_symbol("pkg.foo")
    assert visible is not None and visible.content_hash == "v2"


def test_bulk_load_matches_sequential_upserts() -> None:
    # bulk_load (one rebuild for all units) must produce exactly the same visible state as
    # upserting each unit in turn — the §8.6 fast path is an optimization, not a behavior change.
    a = make_symbol("pkg.a.foo", signature="def foo(x):", unit="pkg/a.py")
    b = make_symbol("pkg.b.bar", signature="def bar(y):", unit="pkg/b.py")
    edge = make_edge(b, a, EdgeType.CALLS)  # cross-unit edge: resolves only if both units load

    seq = InMemoryStore()
    seq.upsert_unit("pkg/a.py", [a], [])
    seq.upsert_unit("pkg/b.py", [b], [edge])

    bulk = InMemoryStore()
    bulk.bulk_load([("pkg/a.py", [a], []), ("pkg/b.py", [b], [edge])])

    assert bulk.visible_node_ids() == seq.visible_node_ids()
    assert bulk.units() == seq.units()
    assert bulk.exists("foo(x)") and bulk.exists("bar(y)")
    # the cross-unit edge is present (both endpoints visible in the single rebuild)
    assert [e.dst for e in bulk.neighbors(b.id, direction=Direction.OUT)] == [a.id]


def test_bulk_load_retains_prior_facts_in_hidden_history() -> None:
    # Re-loading a unit hides its prior facts (kept in history), same as upsert_unit.
    store = InMemoryStore()
    v1 = make_symbol("pkg.foo", signature="def foo(x):", unit="pkg/a.py", content="v1")
    store.bulk_load([("pkg/a.py", [v1], [])])
    v2 = make_symbol("pkg.foo", signature="def foo(x, y):", unit="pkg/a.py", content="v2")
    store.bulk_load([("pkg/a.py", [v2], [])])

    visible = store.get_symbol("pkg.foo")
    assert visible is not None and visible.content_hash == "v2"
    assert v1.id in store.hidden_node_ids()  # prior fact retained, not lost


def test_duplicate_node_id_across_active_units_is_refused() -> None:
    # Silent last-writer-wins would drop a fact without a hidden-history record, breaking
    # the retention guarantee — so the store refuses it loudly instead.
    store = InMemoryStore()
    foo_a = make_symbol("pkg.foo", signature="def foo():", unit="pkg/a.py")
    foo_b = make_symbol("pkg.foo", signature="def foo():", unit="pkg/b.py")  # same id
    store.upsert_unit("pkg/a.py", [foo_a], [])
    with pytest.raises(ValueError, match="duplicate node id"):
        store.upsert_unit("pkg/b.py", [foo_b], [])
    # The rejected mutation is atomic: the store is left exactly as it was.
    assert store.visible_node_ids() == {foo_a.id}
    assert store.get_symbol("pkg.foo") is not None


def test_hide_units_removes_from_visibility() -> None:
    store = InMemoryStore()
    foo = make_symbol("pkg.foo", signature="def foo():", unit="pkg/a.py")
    store.upsert_unit("pkg/a.py", [foo], [])
    store.hide_units(["pkg/a.py"])
    assert store.get_symbol("pkg.foo") is None
    assert foo.id in store.hidden_node_ids()


# --------------------------------------------------------------------------- #
# Typed-edge traversal + EXTRACTED-only filtering
# --------------------------------------------------------------------------- #


def _store_with_mixed_edges() -> tuple[InMemoryStore, Node, Node, Node]:
    store = InMemoryStore()
    a = make_symbol("pkg.a", signature="def a():", unit="pkg/a.py")
    b = make_symbol("pkg.b", signature="def b():", unit="pkg/a.py")
    c = make_symbol("pkg.c", signature="def c():", unit="pkg/a.py")
    e_call = make_edge(a, b, EdgeType.CALLS, Confidence.EXTRACTED)
    e_inferred = make_edge(a, c, EdgeType.INFERRED_RELATES, Confidence.INFERRED)
    store.upsert_unit("pkg/a.py", [a, b, c], [e_call, e_inferred])
    return store, a, b, c


def test_neighbors_filter_by_edge_type() -> None:
    store, a, b, _c = _store_with_mixed_edges()
    out = store.neighbors(a.id, edge_types=[EdgeType.CALLS], direction=Direction.OUT)
    assert len(out) == 1
    assert out[0].type is EdgeType.CALLS
    assert out[0].dst == b.id


def test_callees_honor_extracted_only_filter() -> None:
    store, a, b, c = _store_with_mixed_edges()
    # No confidence filter: both the call target and the inferred relation are reached.
    all_callees = {n.id for n in store.callees_of(a.id, depth=1)}
    assert all_callees == {b.id, c.id}
    # EXTRACTED-only (what the engine passes when gating): the INFERRED edge is excluded.
    extracted = {n.id for n in store.callees_of(a.id, depth=1, confidence=[Confidence.EXTRACTED])}
    assert extracted == {b.id}


def test_callers_of_reverse_direction() -> None:
    store, a, b, _c = _store_with_mixed_edges()
    callers = {n.id for n in store.callers_of(b.id, depth=1)}
    assert callers == {a.id}


def test_subgraph_filters_by_confidence() -> None:
    store, a, b, c = _store_with_mixed_edges()
    sg = store.subgraph([a.id, b.id, c.id], confidence=[Confidence.EXTRACTED])
    edge_types = {e.type for e in sg.edges}
    assert EdgeType.INFERRED_RELATES not in edge_types
    assert EdgeType.CALLS in edge_types


# --------------------------------------------------------------------------- #
# Instrumentation: queries emit query_traversal spans
# --------------------------------------------------------------------------- #


def test_queries_emit_query_traversal_span() -> None:
    recorder = SpanRecorder(rss_sampler=lambda: 1)
    store, a, _b, _c = _store_with_mixed_edges_with_recorder(recorder)
    store.callees_of(a.id, depth=1)
    assert len(recorder.spans) >= 1
    assert all(s.phase is Phase.QUERY_TRAVERSAL for s in recorder.spans)
    # The traversal span is tagged with how many nodes/edges it touched.
    assert any(s.node_count > 0 for s in recorder.spans)


def _store_with_mixed_edges_with_recorder(
    recorder: SpanRecorder,
) -> tuple[InMemoryStore, Node, Node, Node]:
    store = InMemoryStore(recorder=recorder)
    a = make_symbol("pkg.a", signature="def a():", unit="pkg/a.py")
    b = make_symbol("pkg.b", signature="def b():", unit="pkg/a.py")
    c = make_symbol("pkg.c", signature="def c():", unit="pkg/a.py")
    store.upsert_unit(
        "pkg/a.py",
        [a, b, c],
        [make_edge(a, b, EdgeType.CALLS), make_edge(a, c, EdgeType.CALLS)],
    )
    return store, a, b, c
