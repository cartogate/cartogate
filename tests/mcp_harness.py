"""In-memory client/server harness for the MCP integration tests.

mcp 2.0 deleted ``mcp.shared.memory.create_connected_server_and_client_session`` — the SDK
now ships only the raw stream primitive (``create_client_server_memory_streams``) and leaves
the wiring to callers. The tests that helper served are the R6 smoke net: they drive real
``tools/list`` / ``tools/call`` round-trips through the SDK, so a change to the handler
registration contract fails loudly there rather than in a user's editor. That net is worth
keeping, so the convenience wrapper is rebuilt here under its original *name*, and under the
subset of its signature these tests actually use — so the ~24 call sites in
``test_mcp_server.py`` / ``test_mcp_consistency.py`` are unchanged. Everything past ``server``
is keyword-only, deliberately: 1.x took ``read_timeout_seconds`` as its second POSITIONAL
argument, so keyword-only turns a stale positional call into a loud error rather than a silent
mis-binding.

``list_roots_callback`` is new (the 1.x helper had no equivalent): it lets a test answer the
server's ``roots/list`` request, which is the only way to exercise the deferred
workspace-resolution path end-to-end.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.session import ListRootsFnT
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def create_connected_server_and_client_session(
    server: Server[Any],
    *,
    list_roots_callback: ListRootsFnT | None = None,
    raise_exceptions: bool = False,
    read_timeout_seconds: float | None = 60.0,
) -> AsyncGenerator[ClientSession, None]:
    """Run ``server`` over in-memory streams and yield an initialized client session.

    ``raise_exceptions`` defaults to False to match the SDK's own default *and* production
    (:func:`cartogate.mcp.server.main` uses the default): a crashing handler still reaches the
    client as an ``MCPError`` and is logged server-side, while the connection survives — so
    nothing is silent either way, and callers asserting on a tool's *error result* still see
    one. Raising instead kills the connection and bypasses those assertions.

    ``read_timeout_seconds`` bounds every client request. The SDK defaults it to ``None``, so a
    handler that wedges would hang the test — and with no pytest-timeout and no per-job timeout
    in CI, that means a job that hangs to the runner's global limit with no diagnostic. A
    bounded default turns that into a normal, readable test failure.
    """
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async def _serve() -> None:
            await server.run(
                server_read,
                server_write,
                server.create_initialization_options(),
                raise_exceptions=raise_exceptions,
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_serve)
            async with ClientSession(
                client_read,
                client_write,
                read_timeout_seconds=read_timeout_seconds,
                list_roots_callback=list_roots_callback,
            ) as client:
                await client.initialize()
                yield client
            # Runs only after the client is fully torn down (its __aexit__ has returned), so
            # this can't race the client's own shutdown. The server task blocks on its read
            # stream, which the streams context manager closes only on exit — cancel so the
            # group doesn't outlive the test body. No try/finally needed: anyio's TaskGroup
            # cancels its own scope when the body raises, so an exception in the test still
            # tears the server task down.
            task_group.cancel_scope.cancel()
