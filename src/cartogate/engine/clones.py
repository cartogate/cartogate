"""Near-duplicate body detection (F-32) — an advisory copy-paste finder.

The duplicate *gate* (`check_duplicate`) is signature-exact and top-level-only, so it misses a
function that was copied and renamed but kept its body. This groups symbols whose **normalized
body is identical** — a verbatim copy-paste, even across a rename (the body hash excludes the
signature and ignores formatting). It is advisory only (never blocks) and high-confidence: an
identical normalized body is a real copy. (One narrow caveat: whitespace is collapsed everywhere,
including *inside* string literals and comments, so two bodies that differ only in e.g. an embedded
newline-vs-space within a string would collide — rare in practice.) A size floor keeps trivial
one-liners out. Catching renamed-*variable* copies (type-2 clones) is deferred (F-32).
"""

from __future__ import annotations

from collections import defaultdict

from cartogate.schema.enums import Confidence, NodeKind
from cartogate.store.base import StoreInterface

#: Default minimum symbol line-span before a duplicated body is worth reporting — keeps one-line
#: getters/forwarders (legitimately repeated across a codebase) out of the results.
DEFAULT_MIN_LINES = 3


def find_duplicate_bodies(
    store: StoreInterface, *, min_lines: int = DEFAULT_MIN_LINES
) -> list[list[str]]:
    """Groups of symbol qualified-names that share an identical normalized body.

    Only EXTRACTED symbols with a body spanning at least ``min_lines`` are considered (R7: the
    same EXTRACTED-only discipline as every other check). Each returned group is the sorted list of
    qualified names sharing one body; the list of groups is sorted, so output is deterministic.
    A group of one (a unique body) is not returned.
    """
    groups: dict[str, set[str]] = defaultdict(set)
    for node_id in store.visible_node_ids():
        node = store.get_node(node_id)
        if node is None or node.kind is not NodeKind.SYMBOL or node.body_hash is None:
            continue
        if node.confidence is not Confidence.EXTRACTED:
            continue
        if node.location.end_line - node.location.start_line + 1 < min_lines:
            continue
        groups[node.body_hash].add(node.qualified_name)

    return sorted(sorted(qnames) for qnames in groups.values() if len(qnames) > 1)
