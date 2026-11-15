"""A one-tool MCP server that shows which result channel a host passes to its model.

Every successful Cartogate tool call carries its payload twice: as a JSON text block (what a
text-only client reads) and as ``structuredContent`` (what a schema-aware client reads). Whether
the model then pays for one copy or two is the host's choice, and hosts do not document it.

This server answers the question by experiment. Its tool returns DIFFERENT content in the two
channels, so whatever the model repeats back names the channel it was given:

- ``{"channel": "text-block", "fruit": "kiwi"}``          in the text block
- ``{"channel": "structured-content", "fruit": "plum"}``  in ``structuredContent``

See ``README.md`` beside this file for how to run it under a host and for recorded results.
"""

from __future__ import annotations

from typing import Any

import anyio
import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

#: The structured result's shape; advertising it makes a 2.x client treat the tool as structured.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"channel": {"type": "string"}, "fruit": {"type": "string"}},
    "required": ["channel", "fruit"],
}

TEXT_PAYLOAD = '{"channel": "text-block", "fruit": "kiwi"}'
STRUCTURED_PAYLOAD = {"channel": "structured-content", "fruit": "plum"}

server = Server("probe", version="0.0.1")


async def _list_tools(
    ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="probe_both",
                description="Returns a probe result.",
                input_schema={"type": "object", "properties": {}},
                output_schema=SCHEMA,
            )
        ]
    )


async def _call_tool(
    ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=TEXT_PAYLOAD)],
        structured_content=STRUCTURED_PAYLOAD,
    )


server.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, _call_tool)


async def main() -> None:
    """Serve the probe over stdio until the host disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
