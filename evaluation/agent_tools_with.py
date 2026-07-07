"""The *with-Cartogate* arm's tools: the six Cartogate MCP tools, executed in-process.

We reuse the project's own ``TOOL_SPECS`` (already in Anthropic tool-schema shape:
name/description/input_schema) and ``dispatch`` so this arm exercises exactly what an MCP
client would call — no separate definitions to drift.
"""

from __future__ import annotations

from typing import Any

from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch


def tool_schemas() -> list[dict[str, Any]]:
    """The Anthropic-format tool definitions for the with-Cartogate arm."""
    # TOOL_SPECS already use {name, description, input_schema} — the Messages API shape.
    return [dict(spec) for spec in TOOL_SPECS]


def make_executor(tools: CartogateTools):
    """Return ``executor(name, arguments) -> dict`` that runs a Cartogate tool call."""

    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dispatch(tools, name, arguments)

    return _execute
