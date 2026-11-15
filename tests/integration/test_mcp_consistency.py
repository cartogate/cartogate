"""Consistency guards for the MCP surface — keep the tool set self-consistent across the
server, the dispatcher, the daemon, and the per-language gate so the surface can't silently
drift (an agent must be able to reach every advertised capability the same way everywhere)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from tests.mcp_harness import create_connected_server_and_client_session

from cartogate.daemon.server import DAEMON_TOOLS
from cartogate.extract.languages import LANGUAGES
from cartogate.extract.pipeline import index_package
from cartogate.mcp.budget import LISTING_TOTAL_BUDGET, LISTING_VISIBLE_BUDGET
from cartogate.mcp.present import SURFACE_VERSION
from cartogate.mcp.server import _tool_definitions, build_server
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


def test_every_tool_spec_declares_a_unique_title() -> None:
    """The MCP layer builds ``Tool`` objects straight from these specs, so a tool added without a
    title ships a blank entry in whatever picker the client renders. Duplicate titles are just as
    bad: two indistinguishable rows the user has to guess between."""
    # Checked over the ADVERTISED set, not TOOL_SPECS: `set_workspace` is defined in server.py
    # and would otherwise sit outside the guard entirely.
    tools = _tool_definitions(with_set_workspace=True)
    blank = [t.name for t in tools if not (t.title or "").strip()]
    assert not blank, f"tools missing a display title: {blank}"
    titles = [t.title for t in tools]
    assert len(set(titles)) == len(titles), f"duplicate titles: {sorted(titles)}"


def test_every_tool_declares_a_known_safety_class() -> None:
    """``safety`` selects the tool's MCP annotations via a lookup with NO default, so a missing or
    unknown value is a hard failure at server build rather than a silent inheritance of the
    permissive read-only hints. This asserts the specs hold up their end."""
    known = {"query", "runs_user_code"}
    bad = [(s["name"], s.get("safety")) for s in TOOL_SPECS if s.get("safety") not in known]
    assert not bad, f"tools with a missing/unknown safety class: {bad}"


def test_query_tools_never_execute_user_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half of the ``safety`` claim, and the one that would have caught the
    ``contract_status`` mislabel.

    Hosts use ``readOnlyHint`` to AUTO-APPROVE tool calls without prompting the user, so a tool
    wrongly marked ``query`` is a route to running commands unattended. Asserting the annotation
    back off the wire is a tautology — this asserts the behaviour instead, and fails the moment a
    tool declared ``query`` reaches a subprocess.
    """
    import subprocess

    # Build the graph FIRST: indexing legitimately shells out to git, and it is fixture setup,
    # not tool behaviour. The ban applies only to what the dispatch calls below do.
    tools = CartogateTools(_store())

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a tool declared safety='query' executed a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    swept = [spec["name"] for spec in TOOL_SPECS if spec["safety"] == "query"]
    assert len(swept) == len(TOOL_SPECS) - 1, "expected exactly one non-query tool"
    for name in swept:
        dispatch(tools, name, dict(_ANY_ARGS))


def test_every_tool_spec_declares_a_valid_output_schema() -> None:
    """Every tool advertises an ``output_schema`` so validating 2.x clients know the result shape.

    The stakes are higher than for input schemas: the SDK client REJECTS any successful call whose
    ``structured_content`` is missing or non-conforming once a schema is advertised, so a wrong
    schema doesn't degrade gracefully — it breaks the tool for every validating host. Each schema
    must itself be a valid JSON Schema describing an object."""
    import jsonschema

    for spec in TOOL_SPECS:
        schema = spec.get("output_schema")
        assert isinstance(schema, dict), f"{spec['name']} has no output_schema"
        assert schema.get("type") == "object", spec["name"]
        jsonschema.validators.validator_for(schema).check_schema(schema)


def test_dispatch_results_validate_against_the_declared_output_schema() -> None:
    """Every tool's ACTUAL result conforms to its advertised schema — on a hit path AND a miss
    path, because most tools return a different shape when the symbol doesn't resolve. This is the
    executable half of the schema contract; the declaration test above is only the static half."""
    import jsonschema

    tools = CartogateTools(_store())
    hit_args = {
        **_ANY_ARGS,
        "signature": "def authenticate(name):",  # exists in the fixture -> blocked path
        "symbol": "authenticate",
        "qualified_name": "sample_pkg.auth.authenticate",
        "symbols": ["sample_pkg.auth.authenticate"],
        "test": "authenticate",
    }
    miss_args = {
        **_ANY_ARGS,
        "symbol": "zz_missing",
        "qualified_name": "zz_missing",
        "symbols": ["zz_missing"],
        "test": "zz_missing",
        "module": "zz/missing.py",  # repo_map detail-miss (absent from _ANY_ARGS -> overview)
        "line": 999,  # slice: not inside a function -> found=False
    }
    for spec in TOOL_SPECS:
        schema = spec["output_schema"]
        for args in (hit_args, miss_args):
            result = dispatch(tools, spec["name"], dict(args))
            jsonschema.validate(result, schema)


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
                assert result.is_error is False, name
                json.loads(result.content[0].text)  # valid JSON payload

    anyio.run(body)


# --- the tool listing has a size budget ------------------------------------------------------
# `tools/list` is paid for too: every host sends each tool's name, description and input schema
# to the model, and some send it on every turn. The lazy server's listing is the one agents get.


def _lazy_listing() -> list[dict[str, Any]]:
    return [
        tool.model_dump(by_alias=True, exclude_none=True, mode="json")
        for tool in _tool_definitions(with_set_workspace=True)
    ]


def test_lazy_listing_fits_the_budget() -> None:
    listing = _lazy_listing()
    visible = sum(
        len(json.dumps(tool[key]))
        for tool in listing
        for key in ("name", "description", "inputSchema")
    )
    total = len(json.dumps(listing))

    assert visible <= LISTING_VISIBLE_BUDGET, f"model-visible listing is {visible} bytes"
    assert total <= LISTING_TOTAL_BUDGET, f"tools/list payload is {total} bytes"


def test_the_workspace_root_parameter_is_described_once_briefly() -> None:
    """The server instructions explain the parameter; repeating a paragraph in every tool's
    schema cost 4,981 bytes of the listing."""
    for tool in _lazy_listing():
        if tool["name"] == "set_workspace":
            continue
        description = tool["inputSchema"]["properties"]["workspace_root"]["description"]
        assert description == "Absolute path of the workspace root. Pass it on your first call."


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "repo": "/work/repo", "repo_id": "repo", "nodes": 12, "mode": "inproc"},
        {"ok": True, "status": "indexing", "repo": "/work/repo", "elapsed_s": 1.5},
        {"ok": False, "status": "indexing", "elapsed_s": 1.5, "note": "first index in progress"},
        {"ok": False, "error": "not a directory: /nope"},
        {"error": "workspace not set", "action": "set_workspace", "known_workspaces": ["/a"]},
    ],
)
def test_every_lazy_tool_admits_a_session_payload(payload: dict[str, Any]) -> None:
    """The lazy server may answer any tool with workspace guidance or indexing progress instead
    of the tool's result; a validating client must accept that."""
    import jsonschema

    for tool in _lazy_listing():
        jsonschema.validate(payload, tool["outputSchema"])


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": 1},
        # Session-payload fields without `ok` or `error`: the branch must keep its teeth.
        {"note": "x"},
        {"status": "indexing", "repo": "/work/repo"},
    ],
)
def test_a_lazy_tool_still_rejects_a_payload_that_is_neither(payload: dict[str, Any]) -> None:
    import jsonschema

    schema = next(t for t in _lazy_listing() if t["name"] == "find_symbol")["outputSchema"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_set_workspace_keeps_the_fully_described_session_schema() -> None:
    schema = next(t for t in _lazy_listing() if t["name"] == "set_workspace")["outputSchema"]

    assert schema["properties"]["ok"] == {"type": "boolean"}
    assert "description" in schema["properties"]["action"]


def test_an_output_schema_change_comes_with_a_surface_version_bump() -> None:
    """A warm daemon is replaced when it serves another result shape than the MCP server
    advertises, and it is `SURFACE_VERSION` that tells them apart. This pins the advertised
    output schemas to that number: change a schema and the test fails until the version is
    bumped (in `mcp/present.py`) and the pin below is updated with it."""
    digest = hashlib.sha256(
        json.dumps([spec["output_schema"] for spec in TOOL_SPECS], sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    assert (SURFACE_VERSION, digest) == (3, "eb5c2d5ecf7453ef"), (
        "an output schema changed: bump SURFACE_VERSION in src/cartogate/mcp/present.py (if "
        f"that is not done yet) and set this pin to (<the new version>, {digest!r})"
    )
