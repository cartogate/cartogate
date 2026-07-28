"""Hook-payload text extraction — a deliberately LEAF module (no cartogate imports).

Split out of :mod:`cartogate.surfaces` so the write gate's fast path stays cheap. The gate is
wired with a catch-all matcher (it runs on every tool call and decides from the payload), so
the common case — a non-edit tool carrying no source — must exit without paying for the
extractor or the store. Importing ``surfaces`` for this one helper cost ~290ms of a ~400ms
cold run; importing this module costs nothing measurable.

``surfaces`` re-exports both names, so existing callers are unaffected.
"""

from __future__ import annotations

from typing import Any

#: Tool-input keys that may carry the text an agent proposes to write. Covers the common
#: Claude Code / Codex Write/Edit shapes; unrecognized tool schemas yield no text (the gate
#: then allows the call — fail-open, consistent with the PreToolUse posture).
PROPOSED_TEXT_KEYS = ("content", "new_string", "new_str", "text")


def extract_proposed_text(tool_input: dict[str, Any]) -> str:
    """Pull the proposed source text out of a PreToolUse tool-input payload."""
    parts = [tool_input[key] for key in PROPOSED_TEXT_KEYS if isinstance(tool_input.get(key), str)]
    return "\n".join(parts)
