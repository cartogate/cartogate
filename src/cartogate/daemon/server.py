"""The daemon TCP server: a warm graph behind a token-authenticated local socket.

Holds the warm store + :class:`~cartogate.mcp.tools.CartogateTools` and dispatches the full tool
surface (the structural gate plus the advisory/resolved tools — the latter answer for real only on
a ``--resolve`` daemon). Bound to 127.0.0.1 only — "local-only" preserved.

**A query never waits on a refresh.** A request KICKS the git-lazy refresh in the background and
is answered from the current graph immediately — a resolved rebuild of a big repo takes minutes,
and refreshing inline before responding stalled every client past its timeout (the editors showed
tools hanging then dying). The served graph is therefore at most one refresh behind under active
editing; the next request after the rebuild lands sees the fresh graph (eventually consistent, the
same contract as the MCP server's out-of-band refresh loop).

Concurrency is safe without locking reads: the in-memory store swaps its visible graph with a
single atomic assignment, and a refresh installs a brand-new ``CartogateTools`` by one attribute
write, so a query reads either the old or the new graph, never a half-built one. Refreshes are
serialized by a lock + an in-flight flag (no stacking).
"""

from __future__ import annotations

import hmac
import logging
import os
import time

import anyio
from anyio.abc import SocketAttribute, SocketStream, TaskStatus
from anyio.streams.buffered import BufferedByteReceiveStream

from cartogate import __version__
from cartogate.daemon.protocol import ProtocolError, build_error, build_ok, decode, encode
from cartogate.daemon.refresh import RefreshStrategy
from cartogate.instrument import SpanRecorder
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.store import InMemoryStore

_LOG = logging.getLogger("cartogate")

#: Every tool the daemon will dispatch — the full surface, derived from ``TOOL_SPECS`` so it can't
#: drift from the MCP server. Resolved-edge tools (blast_radius / find_references / …) only return
#: real results when the daemon holds the *resolved* graph (started with ``resolve=True``); on a
#: structural daemon they return empty. The discovery file's ``resolve`` flag tells a client which
#: it's talking to, so it can fall back to its own resolved graph when the daemon is structural.
DAEMON_TOOLS = frozenset(str(spec["name"]) for spec in TOOL_SPECS)

#: Reserved request that returns daemon/graph health instead of a tool result (for `doctor`).
HEALTH_TOOL = "__health__"

#: Hard cap on a single request line, so a malformed client can't exhaust memory.
_MAX_REQUEST_BYTES = 1 << 20


class DaemonServer:
    """Serves all registered MCP tools over a local TCP socket (resolved tools need --resolve)."""

    def __init__(
        self,
        root: object,
        *,
        repo_id: str,
        token: str,
        refresh: RefreshStrategy,
        recorder: SpanRecorder | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._root = root
        self._repo_id = repo_id
        self._token = token
        self._refresh = refresh
        self._recorder = recorder
        self._host = host
        self._port = port
        self._tools: CartogateTools | None = None
        self._store: InMemoryStore | None = None
        self._refresh_lock = anyio.Lock()
        self._tg: anyio.abc.TaskGroup | None = None  # set by serve(); hosts background refreshes
        self._refresh_running = False  # event-loop-only flag: at most one kicked refresh in flight
        # Health/observability counters (read by `cartogate doctor` via the health request).
        self._started_at = time.monotonic()
        self._refresh_count = 0
        self._errors = 0
        self._last_error: str | None = None

    async def serve(self, *, task_status: TaskStatus[int] = anyio.TASK_STATUS_IGNORED) -> None:
        """Prime the warm store, bind the socket, report the port, and serve until cancelled."""
        store = await anyio.to_thread.run_sync(self._refresh.prime)
        self._install(store)
        listener = await anyio.create_tcp_listener(local_host=self._host, local_port=self._port)
        bound_port = int(listener.extra(SocketAttribute.local_port))
        task_status.started(bound_port)
        try:
            async with anyio.create_task_group() as tg:
                self._tg = tg  # hosts the kicked background refreshes (cancelled with the server)
                await listener.serve(self._handle, task_group=tg)
        finally:
            self._tg = None  # lifecycle clarity; _kick_refresh is unreachable once handlers stop

    def _install(self, store: InMemoryStore) -> None:
        # Single attribute write — readers see either the old or the new tools, never partial.
        self._store = store
        self._tools = CartogateTools(store)

    def _kick_refresh(self) -> None:
        """Start the git-lazy refresh in the BACKGROUND (at most one in flight) — the request that
        triggered it is answered from the current graph without waiting.

        Staleness has no ENFORCEMENT consequence: the pre-commit gate is always in-process
        (``precommit.py`` indexes the repo itself, never queries the daemon) — the daemon serves
        advisory queries only, where an up-to-one-refresh-stale answer beats a stalled client.
        """
        if self._refresh_running or self._tg is None:
            return
        self._refresh_running = True
        self._tg.start_soon(self._refresh_bg)

    async def _refresh_bg(self) -> None:
        try:
            await self._refresh_if_needed()
        finally:
            self._refresh_running = False

    async def _refresh_if_needed(self) -> None:
        async with self._refresh_lock:
            try:
                # abandon_on_cancel=False (default): cancellation waits for the worker thread —
                # acceptable, since cmd_stop hard-kills the process anyway.
                new_store = await anyio.to_thread.run_sync(self._refresh.maybe_refresh)
            except Exception as exc:  # noqa: BLE001
                # A transient index error must not kill the daemon or drop it offline: keep
                # serving the last good graph and record the failure so `doctor` can surface it.
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                _LOG.warning("daemon refresh failed (serving last good graph): %s", exc)
                return
            if new_store is not None:
                self._install(new_store)
                self._refresh_count += 1

    async def _handle(self, stream: SocketStream) -> None:
        try:
            buffered = BufferedByteReceiveStream(stream)
            # DelimiterNotFound fires when a client sends > _MAX_REQUEST_BYTES with no newline;
            # it must be caught here, or it would escape into listener.serve() and kill the
            # whole daemon. One oversized request must not take the service down.
            line = await buffered.receive_until(b"\n", _MAX_REQUEST_BYTES)
            response = await self._respond(decode(line))
        except (
            ProtocolError,
            anyio.EndOfStream,
            anyio.IncompleteRead,
            anyio.DelimiterNotFound,
            ValueError,
        ):
            response = build_error("bad request")
        try:
            await stream.send(encode(response))
        finally:
            await stream.aclose()

    def _health(self) -> dict[str, object]:
        """Daemon + graph health, surfaced by ``cartogate doctor``."""
        store = self._store
        info = getattr(self._refresh, "last_refresh", None)
        return {
            "repo": str(self._root),
            "repo_id": self._repo_id,
            "version": __version__,
            "pid": os.getpid(),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "nodes": len(store.visible_node_ids()) if store is not None else 0,
            "edges": store.edge_count() if store is not None else 0,
            "units": len(store.units()) if store is not None else 0,
            "refreshes": self._refresh_count,
            "last_refresh": (
                {"mode": info.mode, "reindexed": info.reindexed} if info is not None else None
            ),
            "errors": self._errors,
            "last_error": self._last_error,
        }

    async def _respond(self, request: dict[str, object]) -> dict[str, object]:
        if not hmac.compare_digest(str(request.get("token", "")), self._token):
            return build_error("unauthorized")
        tool = request.get("tool")
        if tool == HEALTH_TOOL:
            self._kick_refresh()  # health reflects the tree as of the last completed refresh
            return build_ok(self._health())
        if not isinstance(tool, str) or tool not in DAEMON_TOOLS:
            return build_error(f"unknown tool {tool!r}")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            return build_error("arguments must be an object")

        # Answer from the CURRENT graph now; the refresh (a full resolved rebuild can take minutes)
        # runs in the background this request just kicked. Blocking here stalled clients to death.
        self._kick_refresh()
        if self._tools is None:  # primed in serve() before serving; defensive (survives -O)
            return build_error("daemon not ready")
        try:
            return build_ok(dispatch(self._tools, tool, arguments))
        except (KeyError, ValueError) as exc:
            return build_error(str(exc))
