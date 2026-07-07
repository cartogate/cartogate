"""Byte-identical tree-sitter helpers shared by every language walker and pure-Python resolver.

A node's source position and text don't depend on the grammar, so these two helpers were copied
verbatim into each of the four walkers (``ast_walker``/``ts_walker``/``java_walker``/``go_walker``)
and the three resolvers (``resolver_ts``/``resolver_java``/``resolver_go``). They live here once
instead — one place to fix, and one fewer thing to re-derive when a fifth language is added.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import blake2b

from tree_sitter import Node as TSNode


def point(node: TSNode) -> tuple[int, int]:
    """A node's start as ``(1-indexed line, 0-indexed column)`` — the position ``RawName`` uses."""
    return node.start_point[0] + 1, node.start_point[1]


def body_hash(body_node: TSNode | None) -> str | None:
    """A whitespace-normalized hash of a callable's body, for near-duplicate (copy-paste) detection.

    Collapses all runs of whitespace to a single space (so re-indented / reformatted copies match)
    but keeps identifiers, literals, and comments — an *identical* normalized body is a verbatim
    copy-paste, even across a function rename (the signature is excluded; this hashes only the body
    block). ``None`` when the node has no ``body`` field (an abstract/extern declaration) or the
    body is empty — those never participate in clone grouping. Classes and interfaces DO carry
    bodies and hash them; the type-decl duplicate gate depends on that.
    """
    if body_node is None or body_node.text is None:
        return None
    normalized = b" ".join(body_node.text.split())
    if not normalized:
        return None
    return blake2b(normalized, digest_size=16).hexdigest()


def text(node: TSNode | None) -> str:
    """A node's source text, UTF-8 decoded; ``""`` for a missing node or missing ``.text``."""
    return node.text.decode("utf-8") if node is not None and node.text is not None else ""


def is_locally_shadowed(
    node: TSNode,
    name: str,
    *,
    is_scope: Callable[[str], bool],
    binds: Callable[[TSNode, str], bool],
) -> bool:
    """Whether ``name`` is bound (param or local) by an enclosing scope — so it is the local, not a
    top-level/package symbol, and must be left unresolved (soundness: never a wrong edge).

    The traversal is the same for every language that *has* shadowable bare names (TypeScript, Go):
    walk ancestor scopes and, at each scope node (``is_scope(node.type)``), ask whether that scope
    ``binds`` the name. The two predicates are the only language-specific parts, so this skeleton
    lives once here — a fifth language gets the guard by supplying its scope set + binding detector
    rather than re-deriving it (the omission of which was a real Go soundness bug). Languages with
    no bare-callable locals (Java) don't need it at all.
    """
    scope: TSNode | None = node.parent
    while scope is not None:
        if is_scope(scope.type) and binds(scope, name):
            return True
        scope = scope.parent
    return False
