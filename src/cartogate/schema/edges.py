"""Graph edge model (spec §4.2).

Edges are typed, directed, immutable facts. Direction is inherent in ``src``/``dst``.
``weight`` (fan-in/out, frequency) is a first-class input to the later MQ clustering
layer. ``confidence`` + ``provenance`` together decide whether an edge may ever drive a
hard BLOCK (the load-bearing rule, spec §4.3).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from cartogate.schema.enums import Confidence, EdgeType, Provenance


class SourceLocation(BaseModel):
    """Where an edge was observed (spec §4.2)."""

    model_config = ConfigDict(frozen=True)

    path: str
    line: int


class Edge(BaseModel):
    """A typed, directed edge between two node ids."""

    model_config = ConfigDict(frozen=True)

    type: EdgeType
    src: str
    dst: str
    weight: float = 1.0
    provenance: Provenance
    confidence: Confidence
    source_location: SourceLocation | None = None


__all__ = ["Edge", "SourceLocation"]
