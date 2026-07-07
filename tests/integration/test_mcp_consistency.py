"""Consistency guards for the MCP surface — keep the tool set self-consistent across the
server, the dispatcher, the daemon, and the per-language gate so the surface can't silently
drift (an agent must be able to reach every advertised capability the same way everywhere)."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from cartogate.daemon.server import DAEMON_TOOLS
from cartogate.extract.languages import LANGUAGES
from cartogate.extract.pipeline import index_package
from cartogate.mcp.server import build_server
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"

#: A superset of every argument any tool reads; `dispatch` picks the ones it needs, so a single
#: dict exercises the routing of every tool without per-tool argument bookkeeping.
_ANY_ARGS = {
    "signature": "def gg_probe():",
    "symbol": "x",
    "qualified_name": "x",
    "symbols": [],
    "test": "x",
    "diff": "",
    "source": "def gg_probe():\n    return 1\n",  # `slice` needs source + line
    "line": 2,
}

_SPEC_NAMES = {spec["name"] for spec in TOOL_SPECS}


def _store() -> InMemoryStore:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    return store


def test_server_list_tools_matches_tool_specs() -> None:
    """The MCP server advertises exactly the ``TOOL_SPECS`` set — no more, no less (data-driven)."""

    async def body() -> None:
        async with create_connected_server_and_client_session(build_server(_store())) as client:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            assert listed == _SPEC_NAMES

    anyio.run(body)


def test_every_tool_spec_dispatches() -> None:
    """Every advertised tool has a working dispatch route (no ``unknown tool`` gap)."""
    tools = CartogateTools(_store())
    for name in _SPEC_NAMES:
        result = dispatch(tools, name, dict(_ANY_ARGS))  # must not raise "unknown tool"
        assert isinstance(result, dict)


def test_unknown_tool_is_rejected() -> None:
    tools = CartogateTools(_store())
    try:
        dispatch(tools, "not_a_tool", {})
    except ValueError as exc:
        assert "unknown tool" in str(exc)
    else:  # pragma: no cover - the guard must raise
        raise AssertionError("dispatch should reject an unknown tool")


def test_daemon_tools_are_a_subset_of_specs() -> None:
    """The daemon serves a subset of the real tools and dispatches them the same way as MCP."""
    assert DAEMON_TOOLS <= _SPEC_NAMES
    assert "check_duplicate" in DAEMON_TOOLS  # the latency-sensitive gate must be daemon-served


def test_check_duplicate_gate_covers_every_registered_language() -> None:
    """The gate's language enum lists every language the extractor can index — so the write-time
    gate fires for all of them, not just a subset."""
    spec = next(s for s in TOOL_SPECS if s["name"] == "check_duplicate")
    enum = set(spec["input_schema"]["properties"]["language"]["enum"])
    registered = {lang.value for lang in LANGUAGES}
    assert registered == enum


def test_server_call_tool_round_trips_for_every_tool() -> None:
    """Each tool round-trips through the real SDK list/call path and returns JSON (R6 smoke)."""

    async def body() -> None:
        async with create_connected_server_and_client_session(build_server(_store())) as client:
            for name in _SPEC_NAMES:
                result = await client.call_tool(name, dict(_ANY_ARGS))
                assert result.isError is False, name
                json.loads(result.content[0].text)  # valid JSON payload

    anyio.run(body)
