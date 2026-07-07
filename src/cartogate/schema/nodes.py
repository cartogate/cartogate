"""Graph node model (spec §4.1).

Nodes are immutable facts (``frozen``): the incremental model hides and stacks whole
units rather than mutating a live node, so a node never changes in place. ``Node.create``
computes the stable id from the id-bearing fields; ``content_hash`` is carried but is
structurally separate from identity (risk R3).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from cartogate.schema.enums import (
    Confidence,
    Granularity,
    Language,
    NodeKind,
    Provenance,
    Visibility,
)
from cartogate.schema.ids import node_id


class Location(BaseModel):
    """Source span of a node (spec §4.1)."""

    model_config = ConfigDict(frozen=True)

    path: str
    start_line: int
    end_line: int


class Authorship(BaseModel):
    """Optional ownership overlay (spec §4.1, Phase 2)."""

    model_config = ConfigDict(frozen=True)

    last_author: str | None = None
    owners: tuple[str, ...] = ()


class Node(BaseModel):
    """A CPG-shaped node. Immutable; identity is derived, not assigned by callers."""

    model_config = ConfigDict(frozen=True)

    id: str
    repo_id: str
    granularity: Granularity = Granularity.SYMBOL
    kind: NodeKind
    name: str
    qualified_name: str
    #: Source language; scopes the duplicate gate and node identity. Defaults to Python so
    #: existing callers and Python-only behaviour are unchanged.
    language: Language = Language.PYTHON
    signature: str | None = None
    location: Location
    unit: str
    module: str | None = None
    layer: str | None = None
    visibility: Visibility = Visibility.INTERNAL
    authorship: Authorship | None = None
    provenance: Provenance
    confidence: Confidence
    content_hash: str
    #: Whitespace-normalized hash of the symbol's body block, for near-duplicate (copy-paste)
    #: detection (F-32). ``None`` when the grammar node has no ``body`` field (modules, externals,
    #: type specs); callables and body-bearing types are included. Carried only; never an input to
    #: the node id (like ``content_hash``).
    body_hash: str | None = None
    #: True for a type declaration (class/interface/struct) vs a callable. Type declarations
    #: sharing a signature are often idiomatic; the duplicate gate blocks them only on a
    #: matching body hash. Default False keeps old snapshots + unannotated walkers on the
    #: original (callable/blocking) behavior.
    is_type_decl: bool = False
    #: Position within the enclosing symbol for statement-granularity nodes; the value
    #: that distinguishes statement ids (CPG-shaped identity). ``None`` for symbols.
    stmt_ordinal: int | None = None
    #: True when the symbol is defined at module scope (a free function or top-level
    #: class). The duplicate gate applies only to these — methods/nested functions share
    #: names across classes legitimately and must not be flagged as duplicate code.
    #: Defaults to ``False`` so the gate FAILS CLOSED: a node created without setting this
    #: is simply not duplicate-checked, rather than risking a false-positive block.
    is_top_level: bool = False

    @classmethod
    def create(
        cls,
        *,
        repo_id: str,
        qualified_name: str,
        kind: NodeKind,
        name: str,
        unit: str,
        location: Location,
        provenance: Provenance,
        confidence: Confidence,
        content_hash: str,
        granularity: Granularity = Granularity.SYMBOL,
        language: Language = Language.PYTHON,
        signature: str | None = None,
        module: str | None = None,
        layer: str | None = None,
        visibility: Visibility = Visibility.INTERNAL,
        authorship: Authorship | None = None,
        stmt_ordinal: int | None = None,
        is_top_level: bool = False,
        is_type_decl: bool = False,
        body_hash: str | None = None,
    ) -> Node:
        """Construct a node with its stable id computed from the id-bearing fields."""
        return cls(
            id=node_id(repo_id, qualified_name, kind, stmt_ordinal, language=language.value),
            repo_id=repo_id,
            granularity=granularity,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            language=language,
            signature=signature,
            location=location,
            unit=unit,
            module=module,
            layer=layer,
            visibility=visibility,
            authorship=authorship,
            provenance=provenance,
            confidence=confidence,
            content_hash=content_hash,
            body_hash=body_hash,
            stmt_ordinal=stmt_ordinal,
            is_top_level=is_top_level,
            is_type_decl=is_type_decl,
        )


__all__ = ["Authorship", "Location", "Node"]
