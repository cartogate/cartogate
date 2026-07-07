"""Stable node identity (spec §4.1, risk R3).

``id = blake2b(repo_id + qualified_name + kind [+ stmt_ordinal])``.

Two invariants make this load-bearing:
- **Content is not an input.** The id deliberately takes no content/body argument, so a
  symbol keeps its identity across edits to its body (``content_hash`` lives elsewhere).
  If content fed identity, every edit would reparent the node and break unit stacking.
- **The scheme is versioned.** ``ID_SCHEME_VERSION`` is folded into the hashed input, so
  any future change to the canonicalization is a deliberate, detectable migration rather
  than a silent re-identification of every node.
"""

from __future__ import annotations

from hashlib import blake2b

#: Bump only as a deliberate identity migration (see module docstring).
#: v2: folded ``language`` into the key so cross-language qualified-name clashes (e.g.
#: ``auth/login.py`` and ``auth/login.ts`` both → ``auth.login``) get distinct ids.
ID_SCHEME_VERSION = 2

#: 16-byte (128-bit) digest: collision-resistant enough that distinct inputs yield
#: distinct ids in any real repo, while keeping ids compact.
_DIGEST_SIZE = 16

#: ASCII unit separator — cannot appear in qualified names, so it is an unambiguous
#: delimiter between the canonical fields (prevents "a|b" vs "ab|" style collisions).
_SEP = "\x1f"


def node_id(
    repo_id: str,
    qualified_name: str,
    kind: str,
    stmt_ordinal: int | None = None,
    *,
    language: str = "python",
) -> str:
    """Compute the stable id for a node.

    Args:
        repo_id: Owning repository id (multi-repo readiness).
        qualified_name: Fully-qualified name within the repo.
        kind: Node kind (a :class:`~cartogate.schema.enums.NodeKind` or its string value).
        stmt_ordinal: Position of a statement within its enclosing symbol, for
            statement-granularity nodes; ``None`` for symbols.

    Returns:
        A hex digest string that is stable across runs for the same inputs.

    Raises:
        ValueError: If ``repo_id`` or ``qualified_name`` is empty — an empty key field
            would silently produce a valid-looking but meaningless id.
    """
    if not repo_id:
        raise ValueError("repo_id must be non-empty")
    if not qualified_name:
        raise ValueError("qualified_name must be non-empty")
    ordinal = "" if stmt_ordinal is None else str(stmt_ordinal)
    canonical = _SEP.join(
        (f"v{ID_SCHEME_VERSION}", repo_id, language, qualified_name, str(kind), ordinal)
    )
    return blake2b(canonical.encode("utf-8"), digest_size=_DIGEST_SIZE).hexdigest()
