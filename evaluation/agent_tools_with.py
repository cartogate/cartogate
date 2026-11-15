"""The *with-Cartogate* arm's tools: the six Cartogate MCP tools, executed in-process.

We reuse the project's own ``TOOL_SPECS`` (already in Anthropic tool-schema shape:
name/description/input_schema) and ``dispatch`` so this arm exercises exactly what an MCP
client would call — no separate definitions to drift.
"""

from __future__ import annotations

from typing import Any

from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch

#: The only keys the Messages API accepts for a custom tool. TOOL_SPECS carries MCP-facing fields
#: too (``title``, and whatever a later MCP feature adds), and the API rejects unknown keys with a
#: 400 — so project them out here rather than passing the spec through. `evaluation/` is outside
#: pytest's testpaths, so a break here is invisible to the suite; the whitelist is what keeps this
#: module immune to the next TOOL_SPECS field.
_MESSAGES_API_KEYS = ("name", "description", "input_schema")


def tool_schemas() -> list[dict[str, Any]]:
    """The Anthropic-format tool definitions for the with-Cartogate arm."""
    return [{k: spec[k] for k in _MESSAGES_API_KEYS} for spec in TOOL_SPECS]


def make_executor(tools: CartogateTools):
    """Return ``executor(name, arguments) -> dict`` that runs a Cartogate tool call."""

    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dispatch(tools, name, arguments)

    return _execute
