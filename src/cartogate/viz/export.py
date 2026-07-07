"""Portable graph serializers: GraphML (Gephi/yEd) and node-link JSON.

Both stamp the same node/edge attributes (kind, qualified name, unit, visibility, confidence,
edge type, provenance) so a viewer can colour/size/filter by them. Output is deterministic
(nodes sorted by id) so diffs and tests are stable. Edges whose endpoints aren't both present
are dropped (the same fail-safe the store applies).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import networkx as nx

from cartogate.schema.edges import Edge
from cartogate.schema.nodes import Node


def _node_attrs(node: Node) -> dict[str, str]:
    return {
        "kind": node.kind.value,
        "qualified_name": node.qualified_name,
        "name": node.name,
        "unit": node.unit,
        "visibility": node.visibility.value,
        "confidence": node.confidence.value,
    }


def _edge_attrs(edge: Edge) -> dict[str, str]:
    return {
        "type": edge.type.value,
        "confidence": edge.confidence.value,
        "provenance": edge.provenance.value,
    }


def _present_edges(edges: Iterable[Edge], node_ids: set[str]) -> list[Edge]:
    return [e for e in edges if e.src in node_ids and e.dst in node_ids]


def to_graphml(nodes: Iterable[Node], edges: Iterable[Edge]) -> str:
    """Serialize the graph to a GraphML document string."""
    node_list = sorted(nodes, key=lambda n: n.id)
    ids = {n.id for n in node_list}
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    for node in node_list:
        graph.add_node(node.id, **_node_attrs(node))
    for edge in _present_edges(edges, ids):
        graph.add_edge(edge.src, edge.dst, **_edge_attrs(edge))
    return "\n".join(nx.generate_graphml(graph, named_key_ids=True))


def to_json(nodes: Iterable[Node], edges: Iterable[Edge]) -> str:
    """Serialize the graph to a deterministic node-link JSON document string."""
    node_list = sorted(nodes, key=lambda n: n.id)
    ids = {n.id for n in node_list}
    payload: dict[str, Any] = {
        "nodes": [{"id": n.id, **_node_attrs(n)} for n in node_list],
        "edges": [
            {"src": e.src, "dst": e.dst, **_edge_attrs(e)}
            for e in sorted(_present_edges(edges, ids), key=lambda e: (e.src, e.dst, e.type.value))
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
