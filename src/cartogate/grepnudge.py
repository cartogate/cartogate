r"""Grep-nudge — PreToolUse advisory hook for symbol-shaped Grep patterns.

Watches the ``Grep`` tool as agents reach for it. When the search pattern looks like a symbol
name (identifier or qualified name), prints advisory context to stdout telling the agent what
cartogate would return instead — without blocking the grep itself.

Scope note: this is wired for the ``Grep`` tool only (a PreToolUse matcher matches tool names).
Bash ``grep``/``rg`` invocations are deliberately NOT hooked — a subprocess spawned before every
Bash call would tax the most-used tool for a marginal nudge; agents are steered to the first-class
Grep tool anyway.

Advisory only: never exits nonzero, never denies permission. The hook reads a PreToolUse
payload on stdin, e.g.::

    {"tool_name": "Grep", "tool_input": {"pattern": "load_config", ...}}

If the pattern qualifies as a symbol (``[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*``
and len >= 3), it prints JSON advisory context to stdout::

    {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "cartogate: 'load_config' looks like a symbol. ..."
    }}

Otherwise prints nothing and exits 0. Infrastructure errors also fail silently (exit 0,
no output) — a missed nudge is free; a wrong nudge would be noise.

Installed as the ``cartogate-grep-nudge`` console script.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# Pattern for a qualified symbol name: [A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*
_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def extract_pattern(payload: dict[str, Any]) -> str | None:
    """Extract the search pattern from a ``Grep`` PreToolUse payload.

    Returns ``tool_input["pattern"]`` for a Grep payload, else None (unrecognized tool or shape).
    """
    if payload.get("tool_name") != "Grep":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    pattern = tool_input.get("pattern")
    return pattern if isinstance(pattern, str) else None


def evaluate(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return advisory context if the pattern qualifies as a symbol, else None.

    The returned dict is shaped for the PreToolUse hook contract: it carries
    additionalContext that the agent will read as advisory guidance without
    blocking the tool call.
    """
    pattern = extract_pattern(payload)
    if not pattern:
        return None

    # A symbol must be 3+ chars and match [A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*
    if len(pattern) < 3 or not _SYMBOL_RE.match(pattern):
        return None

    context = (
        f"cartogate: '{pattern}' looks like a symbol. The graph can answer this "
        f"resolved: find_references('{pattern}') → callers with file:line call sites; "
        f"find_symbol('{pattern}') → definition + signature; "
        f"blast_radius('{pattern}') → what breaks if it changes; "
        f"implementations('{pattern}') → subclasses/implementers. "
        f"grep matches raw text (comments, strings, same-named strangers) — "
        f"fine for non-code text."
    )

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }


def main() -> int:
    """Console-script entry: read PreToolUse payload, evaluate, print advisory if any.

    Always exits 0 — never blocks. JSON errors, missing fields, or non-symbols are
    silent (no output).
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0  # Not a payload we understand — silent.

    if not isinstance(payload, dict):
        return 0

    result = evaluate(payload)
    if result:
        print(json.dumps(result))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
