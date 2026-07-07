"""Daemon wire protocol: one JSON object per line.

Deliberately minimal and dependency-free (no MCP HTTP stack). A request names a tool and its
arguments and carries the shared token; a response is either ``{ok: true, result}`` or
``{ok: false, error}``. The ``tool``/``arguments`` pair feeds the existing
:func:`cartogate.mcp.tools.dispatch` unchanged.
"""

from __future__ import annotations

import json
from typing import Any


class ProtocolError(ValueError):
    """Raised when a message cannot be parsed as a single JSON object."""


def encode(message: dict[str, Any]) -> bytes:
    """Serialize a message as one newline-terminated JSON line."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(data: bytes | str) -> dict[str, Any]:
    """Parse one message; raise :class:`ProtocolError` if it is not a JSON object."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("message must be a JSON object")
    return parsed


def build_request(token: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a tool-call request."""
    return {"token": token, "tool": tool, "arguments": arguments}


def build_ok(result: dict[str, Any]) -> dict[str, Any]:
    """Build a success response carrying the tool result."""
    return {"ok": True, "result": result}


def build_error(message: str) -> dict[str, Any]:
    """Build an error response."""
    return {"ok": False, "error": message}
