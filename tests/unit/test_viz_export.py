"""Tests for the GraphML + JSON graph exporters."""

from __future__ import annotations

import json

import networkx as nx
from tests.conftest import MakeSymbol

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, Provenance
from cartogate.viz.export import to_graphml, to_json


def _edge(src: str, dst: str, edge_type: EdgeType) -> Edge:
    return Edge(
        type=edge_type, src=src, dst=dst, provenance=Provenance.LSP, confidence=Confidence.EXTRACTED
    )


def test_to_graphml_round_trips_nodes_edges_and_attrs(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.a", signature="def a():", unit="pkg/a.py")
    b = make_symbol("pkg.b", signature="def b():", unit="pkg/b.py")
    graphml = to_graphml([a, b], [_edge(a.id, b.id, EdgeType.CALLS)])

    graph = nx.parse_graphml(graphml, force_multigraph=True)
    assert set(graph.nodes) == {a.id, b.id}
    assert graph.nodes[a.id]["kind"] == "symbol"
    assert graph.nodes[a.id]["qualified_name"] == "pkg.a"
    edge_attrs = next(iter(graph.get_edge_data(a.id, b.id).values()))
    assert edge_attrs["type"] == "calls"


def test_to_graphml_drops_dangling_edges(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.a", unit="pkg/a.py")
    graphml = to_graphml([a], [_edge(a.id, "missing-id", EdgeType.CALLS)])
    graph = nx.parse_graphml(graphml)
    assert graph.number_of_edges() == 0  # endpoint not present -> dropped


def test_to_json_is_deterministic_node_link(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.a", unit="pkg/a.py")
    b = make_symbol("pkg.b", unit="pkg/b.py")
    edges = [_edge(a.id, b.id, EdgeType.CALLS)]
    out1 = to_json([a, b], edges)
    out2 = to_json([b, a], edges)  # input order shouldn't change output
    assert out1 == out2
    payload = json.loads(out1)
    assert {n["qualified_name"] for n in payload["nodes"]} == {"pkg.a", "pkg.b"}
    assert payload["edges"][0]["type"] == "calls"
