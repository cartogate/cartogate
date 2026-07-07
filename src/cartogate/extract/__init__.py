"""Structural extraction: tree-sitter AST + jedi name resolution → EXTRACTED facts.

The deterministic, model-free pass that populates the v0 structural slice of the graph
(spec §5.1). Semantic/inferred extraction is a later phase and never gates.
"""

from cartogate.extract.pipeline import IndexResult, index_package
from cartogate.extract.scip_emit import emit_scip

__all__ = ["IndexResult", "emit_scip", "index_package"]
