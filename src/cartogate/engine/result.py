"""Gate result types.

A ``BlockResult`` is the verdict of the only hard-enforcement mode in v0. It is a plain
data record so every surface (MCP tool, git pre-commit, PreToolUse hook) can render it
the same way and decide enforcement uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BlockKind(StrEnum):
    """Why a BLOCK decision was reached (or not)."""

    OK = "ok"
    DUPLICATE = "duplicate"
    CONTRACT = "contract"


@dataclass(frozen=True, slots=True)
class BlockResult:
    """The verdict of a BLOCK check."""

    blocked: bool
    kind: BlockKind
    reason: str = ""
    existing_symbol_id: str | None = None
    existing_qualified_name: str | None = None
    #: ``path:line`` of the existing symbol, so a blocked agent can jump straight to it instead of
    #: re-searching (F-66). ``None`` when not a duplicate.
    existing_location: str | None = None
    #: The existing symbol's raw (un-normalized) signature — what to reuse. ``None`` when not a
    #: duplicate. (``reason`` embeds the *normalized* form; this is the source spelling.)
    existing_signature: str | None = None

    @classmethod
    def ok(cls) -> BlockResult:
        return cls(blocked=False, kind=BlockKind.OK)

    def action(self) -> str:
        """The ONE sanctioned next step for the agent that was blocked."""
        if not self.blocked:
            return ""
        if self.kind is BlockKind.DUPLICATE:
            target = self.existing_qualified_name or "the existing symbol"
            return f"reuse {target} — import/call it instead of re-implementing it."
        target = self.existing_qualified_name or "the symbol"
        return (
            f"keep the existing contract, or update every caller of {target} in the same "
            f'change — run find_references("{target}") to list them.'
        )

    def agent_message(self) -> str:
        """The full agent-facing block message.

        Shape (STRATEGY.md design law 1 — the measured retry-loop killer): BLOCKED (what) →
        EVIDENCE (the extracted fact, with file:line) → ACTION (the one sanctioned step) →
        anti-loop ("do NOT retry identical / do not rename to evade"). The message asserts
        only EXTRACTED facts, so it can speak with certainty.
        """
        if not self.blocked:
            return ""
        where = f" ({self.existing_location})" if self.existing_location else ""
        if self.kind is BlockKind.DUPLICATE:
            # Signatures are raw source slices: fold newlines and backticks so the inline code
            # span renders as one line (cosmetic only; the facts are unaffected).
            sig = " ".join((self.existing_signature or "").replace("`", "'").split())
            defined = f", defined as `{sig}`" if sig else ""
            what = "creating this symbol would duplicate an existing one."
            evidence = (
                f"same normalized signature as {self.existing_qualified_name}{where}{defined}."
            )
            tail = (
                "Do NOT retry with identical arguments, and do NOT rename the new symbol to "
                "evade this gate — the fix is reuse, not a new name."
            )
        else:
            what = (
                "this change breaks the established contract of "
                f"{self.existing_qualified_name or 'an existing symbol'}."
            )
            evidence = f"{self.reason}{where}."
            tail = "Do NOT retry with identical arguments."
        return (
            f"BLOCKED: {what}\n"
            f"EVIDENCE (EXTRACTED): {evidence}\n"
            f"ACTION: {self.action()}\n"
            f"{tail}"
        )
