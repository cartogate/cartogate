"""Controlled vocabularies for the CPG-shaped property graph (spec §4).

The schema is engineered to *grow toward a full CPG* without a rewrite: the node and
edge vocabularies already declare the statement/predicate granularity and the CFG/PDG
edge slots, even though v0 only populates the structural slice. Keeping the slots
reserved here is what makes adding depth later additive rather than a migration.
"""

from __future__ import annotations

from enum import StrEnum


class Granularity(StrEnum):
    """Node granularity. Statement-level identity is available now (CPG-shaped)."""

    SYMBOL = "symbol"
    STATEMENT = "statement"


class Language(StrEnum):
    """Source language of an extracted node. Scopes the duplicate gate and node identity so a
    Python ``add(a,b)`` and a TypeScript ``add(a,b)`` are never confused for each other."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVA = "java"
    GO = "go"
    RUST = "rust"
    JAVASCRIPT = "javascript"
    CSHARP = "csharp"
    C = "c"
    CPP = "cpp"
    KOTLIN = "kotlin"
    SWIFT = "swift"


class NodeKind(StrEnum):
    """What a node represents (spec §4.1)."""

    MODULE = "module"
    FILE = "file"
    SYMBOL = "symbol"
    STATEMENT = "statement"
    PREDICATE = "predicate"
    DOC_SECTION = "doc_section"
    TEST = "test"
    EXTERNAL_PACKAGE = "external_package"
    API_ENDPOINT = "api_endpoint"


class EdgeType(StrEnum):
    """Typed, directed edges (spec §4.2).

    Tiers: v0 structural edges are populated now; CPG-depth and analysis/cross-repo
    edges are reserved slots populated in later phases; ``inferred_relates`` is the
    advisory semantic edge that must never reach the BLOCK gate.
    """

    # --- v0 deterministic structural ---
    CALLS = "calls"
    IMPORTS = "imports"
    DEPENDS_ON = "depends_on"
    DEFINES = "defines"
    REFERENCES = "references"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    # --- CPG depth (Phase 2 / lazy) — reserved, not populated in v0 ---
    CONTROL_FLOW = "control_flow"
    CONTROL_DEP = "control_dep"
    DATA_DEP = "data_dep"
    # --- analysis (Phase 1–2) ---
    TESTS = "tests"
    DOCUMENTS = "documents"
    EXPOSES = "exposes"
    # --- cross-repo (Phase 3) ---
    CONSUMES = "consumes"
    # --- advisory semantic ---
    INFERRED_RELATES = "inferred_relates"


class Provenance(StrEnum):
    """Where a fact came from. Drives the enforcement rule together with confidence."""

    TREE_SITTER = "tree-sitter"
    LSP = "lsp"
    STACK_GRAPHS = "stack-graphs"
    CFG = "cfg"
    PDG = "pdg"
    MANIFEST = "manifest"
    DOC = "doc"  # explicit references parsed from documentation (deterministic, but advisory)
    SEMANTIC_SKILL = "semantic-skill"
    GRAPHIFY = "graphify"


class Confidence(StrEnum):
    """Confidence tier. The load-bearing gate rule (spec §4.3) keys on this."""

    EXTRACTED = "EXTRACTED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"


class Visibility(StrEnum):
    """Public-surface marking (spec §4.1) — feeds contract checks and cross-repo ports."""

    PUBLIC = "public"
    EXPORTED = "exported"
    INTERNAL = "internal"


#: Provenances whose EXTRACTED facts are allowed to drive a hard BLOCK (spec §4.3).
#: cfg/pdg are deterministic but over-approximate, so they are excluded even though they
#: are EXTRACTED. INFORMATIONAL reference for the spec rule: in v0 the actual enforcement
#: is by edge type (``cartogate.engine.traversal.GATE_EDGE_TYPES``, which excludes the
#: control_flow/control_dep/data_dep slots). When cfg/pdg provenances start producing
#: facts (Phase 2), the traversal chokepoint must additionally filter on this set.
BLOCKABLE_PROVENANCES: frozenset[Provenance] = frozenset(
    {Provenance.TREE_SITTER, Provenance.LSP, Provenance.STACK_GRAPHS, Provenance.MANIFEST}
)
