"""The deterministic gate engine: diff → changed symbols → typed traversal → result.

v0 ships the BLOCK and INVALIDATE modes (spec §1). FLAG and LOCALIZE are later phases
(F-01, F-02). All gate traversals go through the single EXTRACTED-only chokepoint in
:mod:`cartogate.engine.traversal` (risk R7).
"""

from cartogate.engine.block import BlockEngine
from cartogate.engine.diff import git_diff_regions, parse_unified_diff
from cartogate.engine.invalidate import Invalidator
from cartogate.engine.result import BlockKind, BlockResult
from cartogate.engine.traversal import GATE_EDGE_TYPES, GatingTraversal

__all__ = [
    "GATE_EDGE_TYPES",
    "BlockEngine",
    "BlockKind",
    "BlockResult",
    "GatingTraversal",
    "Invalidator",
    "git_diff_regions",
    "parse_unified_diff",
]
