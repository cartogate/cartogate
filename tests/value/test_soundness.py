"""V9 — soundness: a BLOCK always rests on EXTRACTED, top-level structural evidence.

Two guarantees, both measured:

1. **R7 — INFERRED never gates.** A colliding symbol that is only INFERRED must not block;
   an EXTRACTED collision does. This is what makes every refusal pointable to source.
2. **Top-level scoping avoids method false-positives.** Methods of different classes that
   share a name/shape are correctly excluded from the duplicate index — measured on
   Cartogate's own source (the dogfood P0 class), while a genuine top-level collision blocks.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from cartogate.engine.block import BlockEngine
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import (
    Confidence,
    NodeKind,
    Provenance,
    Visibility,
)
from cartogate.schema.nodes import Location, Node
from cartogate.schema.signature import normalize_signature
from cartogate.store import InMemoryStore

from .metrics import COLLECTOR

pytestmark = pytest.mark.value

SELF_SRC = Path(__file__).resolve().parents[2] / "src" / "cartogate"


def _symbol(qname: str, signature: str, *, confidence: Confidence) -> Node:
    unit = qname.rsplit(".", 1)[0].replace(".", "/") + ".py"
    return Node.create(
        repo_id="t",
        qualified_name=qname,
        kind=NodeKind.SYMBOL,
        name=qname.rsplit(".", 1)[-1],
        unit=unit,
        signature=signature,
        location=Location(path=unit, start_line=1, end_line=2),
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=confidence,
        content_hash=qname,
        is_top_level=True,
    )


def test_inferred_facts_never_block() -> None:
    store = InMemoryStore()
    store.upsert_unit(
        "ghost.py", [_symbol("ghost.spook", "def spook(x):", confidence=Confidence.INFERRED)], []
    )
    store.upsert_unit(
        "real.py", [_symbol("real.solid", "def solid(x):", confidence=Confidence.EXTRACTED)], []
    )
    engine = BlockEngine(store)

    # The INFERRED collision must NOT block (R7); the EXTRACTED one must (control).
    assert engine.check_duplicate("def spook(x):").blocked is False
    assert engine.check_duplicate("def solid(x):").blocked is True


def test_top_level_scoping_excludes_method_false_positives() -> None:
    store = InMemoryStore()
    result = index_package(
        SELF_SRC, repo_id="cartogate", store=store, resolve=False, index_docs=False
    )
    tools = CartogateTools(store)

    methods = [
        n
        for n in result.nodes
        if n.kind is NodeKind.SYMBOL and n.signature and not n.is_top_level
    ]
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for n in methods:
        key = (n.language.value, normalize_signature(n.signature, n.language))
        groups[key].add(n.qualified_name)
    excluded_groups = {k: v for k, v in groups.items() if len(v) > 1}
    excluded_symbols = sum(len(v) for v in excluded_groups.values())

    # Method signatures that collide across classes are excluded from the duplicate index...
    assert not tools.check_duplicate("def get_symbol(qualified_name):")["blocked"]
    assert not tools.check_duplicate("def to_dict():")["blocked"]
    # ...while a genuine top-level free-function collision still blocks.
    assert tools.check_duplicate("def main(argv):")["blocked"]
    assert excluded_groups, "expected method-signature collisions to exist on the real source"

    COLLECTOR.record(
        hypothesis="V9",
        bucket="C",
        title="Soundness (R7 + top-level scoping)",
        claim="A BLOCK always rests on EXTRACTED, top-level evidence: INFERRED facts never gate, "
        "and method-name collisions across classes are never false-blocked.",
        metric={
            "inferred_never_blocks": True,
            "method_collision_groups_excluded": len(excluded_groups),
            "method_symbols_excluded": excluded_symbols,
            "method_symbols_total": len(methods),
            "real_top_level_collision_blocks": True,
            "self_source": "src/cartogate",
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_soundness.py",
        notes="On Cartogate's own source, top-level scoping excludes "
        "method-name collisions that a naive signature index would false-block.",
    )
