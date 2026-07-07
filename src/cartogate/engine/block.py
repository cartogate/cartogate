"""BLOCK mode — the only hard-enforcement gate in v0 (spec §1, §13).

Two deterministic checks, both resting solely on EXTRACTED structural facts:

- **Duplicate** — a new symbol whose normalized signature already exists. This is the
  v0 success criterion: an agent asked to add a function that already exists gets the hit
  and reuses it instead of duplicating.
- **Contract** — a change to an existing symbol's signature or a narrowing of its
  visibility (public → exported → internal). Breaking an exported contract is refused.

Neither check can be influenced by INFERRED facts: ``exists``/``find_symbols_by_signature``
index only symbol nodes (all EXTRACTED in v0), and visibility/signature come from the
symbol's own EXTRACTED fields.
"""

from __future__ import annotations

from cartogate.engine.result import BlockKind, BlockResult
from cartogate.schema.enums import Language, Visibility
from cartogate.schema.signature import normalize_signature
from cartogate.store.base import StoreInterface

#: Higher = broader public surface. Narrowing (a lower rank) breaks a contract.
_VISIBILITY_RANK = {
    Visibility.INTERNAL: 0,
    Visibility.EXPORTED: 1,
    Visibility.PUBLIC: 2,
}


class BlockEngine:
    """Computes BLOCK verdicts against the current graph."""

    def __init__(self, store: StoreInterface) -> None:
        self._store = store

    def check_duplicate(
        self,
        signature: str,
        language: Language = Language.PYTHON,
        *,
        exclude_unit: str | None = None,
        proposed_body_hash: str | None = None,
        proposed_is_type_decl: bool | None = None,
    ) -> BlockResult:
        """Block if a symbol with the same normalized signature already exists in ``language``.

        Call contract: this is a check for a *new* symbol being introduced. When an agent is
        *editing* an existing symbol, pass ``exclude_unit`` = the unit (file) being edited: a
        matching symbol that lives in that file is the one being edited, not a new duplicate, so
        it is ignored. A match in any *other* file is still a real cross-file duplicate. (Two
        same-signature defs *within* the edited file are not caught — same-file shadowing is the
        author's own concern, not cross-file structural duplication.)
        """
        normalized = normalize_signature(signature, language)
        hits = self._store.find_symbols_by_signature(signature, language)
        if exclude_unit is not None:
            hits = [hit for hit in hits if hit.unit != exclude_unit]
        # Like compares with like (review of task #24): a callable and a type declaration
        # sharing a signature is a naming coincidence, never gate evidence. Callables block on
        # signature alone (a re-implementation is the core catch); TYPE DECLARATIONS block only
        # on a matching body hash — name+bases is idiomatic (React Props, per-service Settings).
        callable_hits = [hit for hit in hits if not hit.is_type_decl]
        type_hits = [hit for hit in hits if hit.is_type_decl]
        if proposed_is_type_decl is True:
            candidates = []  # a proposed type decl is never judged against callable homonyms
        elif proposed_is_type_decl is False:
            candidates = callable_hits
            type_hits = []  # ...nor a proposed callable against type-decl homonyms
        else:
            candidates = callable_hits  # kind unknown (raw MCP): callables keep the old rule
        if not candidates:
            copy_paste = [
                hit
                for hit in type_hits
                if proposed_body_hash is not None and hit.body_hash == proposed_body_hash
            ]
            if copy_paste:
                candidates = copy_paste
            elif type_hits:
                # Signature-only type-decl match: NOT blocked, but say what exists — the
                # interactive check_duplicate caller gets a near-match to inspect instead of a
                # silent (and misleading) all-clear. Write/commit gates verify bodies for real.
                near = min(type_hits, key=lambda n: n.id)
                return BlockResult(
                    blocked=False,
                    kind=BlockKind.OK,
                    existing_symbol_id=near.id,
                    existing_qualified_name=near.qualified_name,
                    existing_location=f"{near.location.path}:{near.location.start_line}",
                    existing_signature=near.signature,
                )
        hits = candidates
        if not hits:
            return BlockResult.ok()
        # Deterministic choice of the reported hit (smallest id) when several collide.
        hit = min(hits, key=lambda n: n.id)
        location = f"{hit.location.path}:{hit.location.start_line}"
        return BlockResult(
            blocked=True,
            kind=BlockKind.DUPLICATE,
            reason=(
                f"a symbol with signature {normalized!r} already exists: "
                f"{hit.qualified_name} ({location}) — reuse it"
            ),
            existing_symbol_id=hit.id,
            existing_qualified_name=hit.qualified_name,
            existing_location=location,
            existing_signature=hit.signature,
        )

    def check_contract(
        self,
        qualified_name: str,
        *,
        new_signature: str | None = None,
        new_visibility: Visibility | None = None,
    ) -> BlockResult:
        """Block if the change alters an existing symbol's signature or narrows visibility.

        Returns the first breach detected (signature is checked before visibility). If the
        existing symbol has no recorded signature, the signature check is skipped (there is
        no established contract to break) and only visibility is considered.
        """
        existing = self._store.get_symbol(qualified_name)
        if existing is None:
            return BlockResult.ok()  # brand-new symbol: no contract to break
        location = f"{existing.location.path}:{existing.location.start_line}"

        if new_signature is not None and existing.signature is not None:
            old_sig = normalize_signature(existing.signature, existing.language)
            new_sig = normalize_signature(new_signature, existing.language)
            if old_sig != new_sig:
                return BlockResult(
                    blocked=True,
                    kind=BlockKind.CONTRACT,
                    reason=f"signature of {qualified_name} changed from {old_sig!r} to {new_sig!r}",
                    existing_symbol_id=existing.id,
                    existing_qualified_name=qualified_name,
                    existing_location=location,
                    existing_signature=existing.signature,
                )

        if (
            new_visibility is not None
            and _VISIBILITY_RANK[new_visibility] < _VISIBILITY_RANK[existing.visibility]
        ):
            return BlockResult(
                blocked=True,
                kind=BlockKind.CONTRACT,
                reason=(
                    f"visibility of {qualified_name} narrowed from "
                    f"{existing.visibility.value} to {new_visibility.value}"
                ),
                existing_symbol_id=existing.id,
                existing_qualified_name=qualified_name,
                existing_location=location,
                existing_signature=existing.signature,
            )

        return BlockResult.ok()
