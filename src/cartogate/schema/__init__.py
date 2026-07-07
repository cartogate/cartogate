"""CPG-shaped schema: nodes, edges, controlled vocabularies, identity, signatures.

The schema is intentionally CPG-shaped (statement identity + reserved CFG/PDG edge
slots) so depth can be added later without a rewrite; v0 only populates the structural
slice. See the spec §3–§4.
"""

from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import (
    BLOCKABLE_PROVENANCES,
    Confidence,
    EdgeType,
    Granularity,
    NodeKind,
    Provenance,
    Visibility,
)
from cartogate.schema.ids import ID_SCHEME_VERSION, node_id
from cartogate.schema.nodes import Authorship, Location, Node
from cartogate.schema.signature import normalize_signature

__all__ = [
    "BLOCKABLE_PROVENANCES",
    "ID_SCHEME_VERSION",
    "Authorship",
    "Confidence",
    "Edge",
    "EdgeType",
    "Granularity",
    "Location",
    "Node",
    "NodeKind",
    "Provenance",
    "SourceLocation",
    "Visibility",
    "node_id",
    "normalize_signature",
]
