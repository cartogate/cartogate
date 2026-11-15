"""Section 4 — the MCP server driven end-to-end via the in-memory client harness.

This is the R6 smoke test: it exercises the real list_tools/call_tool round-trip through
the SDK, so an SDK API change that breaks handler registration fails loudly here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from tests.mcp_harness import create_connected_server_and_client_session

import cartogate
from cartogate.daemon import client as daemon_client
from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.extract.pipeline import index_package
from cartogate.mcp import server as mcp_server
from cartogate.mcp.present import SURFACE_VERSION
from cartogate.mcp.server import (
    _no_daemon_requested,
    _refresh_loop,
    build_lazy_server,
    build_server,
)
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"


def _store() -> InMemoryStore:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    return store


def test_server_lists_tools_and_dispatches_check_duplicate() -> None:
    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "check_duplicate",
                "blast_radius",
                "find_symbol",
                "find_references",
                "suggest_tests",
                "doc_drift",
                "find_cycles",
                "find_duplicate_bodies",
                "find_dead_code",
                "read_symbol",
                "implementations",
                "repo_map",
                "impact_summary",
                "localize",
                "slice",
                "contract_status",
                "gate_history",
            }

            result = await client.call_tool(
                "check_duplicate", {"signature": "def authenticate(name):"}
            )
            assert result.is_error is False
            payload = json.loads(result.content[0].text)
            assert payload["blocked"] is True
            assert payload["existing_qualified_name"] == "sample_pkg.auth.authenticate"

    anyio.run(body)


def test_set_workspace_activates_a_deferred_server(tmp_path: Path) -> None:
    """The agent-provided-root path: a deferred server that can't self-resolve is pointed at a repo
    by the `set_workspace` tool, after which normal tools serve that repo's graph."""
    import subprocess

    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)

    async def body() -> None:
        async def _never_resolves(_session: object) -> None:
            return None  # editor gives no signal -> the agent must set_workspace

        server, _warm, _refresh = build_lazy_server(None, None, resolver=_never_resolves)
        async with create_connected_server_and_client_session(server) as client:
            # Before set_workspace: a tool call reports it needs the workspace.
            err = json.loads((await client.call_tool("find_cycles", {})).content[0].text)
            assert err["action"] == "set_workspace"

            # The agent supplies the root -> the server indexes it.
            activated = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert activated["ok"] is True and activated["repo_id"] == "proj"

            # Now a normal tool serves that repo's graph.
            found = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "proj.pkg.a.helper"}))
                .content[0].text
            )
            assert found["found"] is True

    anyio.run(body)


def _tiny_git_repo(root: Path, func: str) -> None:
    import subprocess

    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(f"def {func}():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)


def test_client_roots_resolve_the_workspace_end_to_end(tmp_path: Path) -> None:
    """The roots rung driven over the real wire: an unresolved server asks the CLIENT which
    workspace this window is (a server->client request issued through the session the SDK hands
    the handler), resolves from the answer, indexes, and serves the tool.

    Every other roots test drives ``_repo_from_client_roots`` with a hand-built fake session, so
    none of them touch the real one — which is how a broken session handle passed a green suite
    once already. This is the test that would not have."""
    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "helper")
    # Empty env + a non-repo cwd neutralize the later rungs (env / guarded cwd), and the autouse
    # CARTOGATE_HOME fixture empties the registry rung — so roots is the only rung that can win,
    # and a pass means roots specifically resolved.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    resolver = mcp_server._make_deferred_resolver({}, elsewhere)

    async def body() -> None:
        async def _roots(context: object) -> types.ListRootsResult:
            return types.ListRootsResult(roots=[types.Root(uri=repo.as_uri(), name="proj")])

        server, _warm, _refresh = build_lazy_server(None, None, resolver=resolver)
        async with create_connected_server_and_client_session(
            server, list_roots_callback=_roots
        ) as client:
            result = await client.call_tool(
                "find_symbol", {"qualified_name": "proj.pkg.a.helper"}
            )
            assert result.is_error is False
            assert json.loads(result.content[0].text)["found"] is True

    anyio.run(body)


def test_crash_visibility_writes_native_fault_dumps_to_a_file(tmp_path: Path) -> None:
    """faulthandler must be armed against a crash.log next to the mcp log — a native fault
    (tree-sitter/jedi segfault) is the one death Python logging can never record."""
    import faulthandler

    was_enabled = faulthandler.is_enabled()
    try:
        assert mcp_server.enable_crash_visibility(tmp_path / ".cartogate" / "mcp.log") is True
        assert faulthandler.is_enabled()
        # The dump target actually works: writing a traceback lands in crash.log.
        faulthandler.dump_traceback(file=mcp_server._CRASH_LOG_FH)
        mcp_server._CRASH_LOG_FH.flush()
        assert "Current thread" in (tmp_path / ".cartogate" / "crash.log").read_text(
            encoding="utf-8", errors="replace"
        )
    finally:
        if was_enabled:
            faulthandler.enable()  # restore the default (stderr) target, not our tmp file
        else:
            faulthandler.disable()


def test_run_and_account_exit_logs_both_endings(caplog) -> None:  # type: ignore[no-untyped-def]
    """The log must always say how the server ended: FATAL + traceback on an unhandled exception
    (re-raised unchanged), or the clean transport-closed line on a normal return."""
    import logging

    import pytest

    async def _boom() -> None:
        raise RuntimeError("kaput")

    with caplog.at_level(logging.INFO, logger="cartogate"):
        with pytest.raises(RuntimeError, match="kaput"):
            mcp_server._run_and_account_exit(_boom)
        assert any("FATAL" in r.message for r in caplog.records)

        async def _clean() -> None:
            return None

        caplog.clear()
        mcp_server._run_and_account_exit(_clean)
        assert any("exiting cleanly" in r.message for r in caplog.records)


def test_crash_visibility_fallbacks_never_block_startup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import builtins
    import faulthandler

    was_enabled = faulthandler.is_enabled()
    try:
        # No log file -> stderr-only arming, reported False.
        assert mcp_server.enable_crash_visibility(None) is False
        assert faulthandler.is_enabled()
        # An unwritable path -> OSError swallowed, stderr fallback, reported False.
        real_open = builtins.open

        def _deny(*a: object, **k: object) -> object:
            raise OSError("read-only")

        monkeypatch.setattr(builtins, "open", _deny)
        assert mcp_server.enable_crash_visibility(Path("/nope/mcp.log")) is False
        monkeypatch.setattr(builtins, "open", real_open)
        assert faulthandler.is_enabled()
    finally:
        if was_enabled:
            faulthandler.enable()
        else:
            faulthandler.disable()


def test_unresolved_session_autoconnects_to_the_only_live_daemon(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Stage-1 daemon-first: a fresh session with NO editor signal and NO agent help finds its
    workspace via the registry when exactly one registered repo has a live resolved daemon —
    sessions survive editor restarts without re-teaching the agent."""
    repo = tmp_path / "proj"
    repo.mkdir()
    monkeypatch.setattr(mcp_server.registry, "live_daemon_workspaces", lambda: [repo.resolve()])
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _r: True)
    seen: list[str] = []

    def _fake_query(_repo: Path, name: str, _arguments: dict, **_k: object) -> dict:
        seen.append(name)
        # Shaped like a real daemon answer (same dispatch, same output_schema): the SDK client
        # validates every success result against the advertised schema, stub or not.
        return {"found": True, "qualified_name": "x", "tool": name}

    monkeypatch.setattr(mcp_server.daemon_client, "query", _fake_query)

    no_signal_cwd = tmp_path / "editorish"  # no root markers anywhere above tmp -> cwd rung misses
    no_signal_cwd.mkdir()

    async def body() -> None:
        # The REAL deferred resolver, with no roots/env/cwd signal — only the registry rung fires.
        resolver = mcp_server._make_deferred_resolver({}, cwd=no_signal_cwd)
        server, _warm, _refresh = build_lazy_server(None, None, resolver=resolver)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "x"})).content[0].text
            )
            assert res["found"] is True and res["tool"] == "find_symbol"  # served via the daemon
            assert seen == ["find_symbol"]

    anyio.run(body)


def test_roots_signal_beats_the_registry_rung(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A genuine per-window signal (MCP roots) must win over the registry memory — the registry is
    a fallback for editors that give nothing, never an override."""
    from types import SimpleNamespace

    roots_repo = tmp_path / "from_roots"
    _tiny_git_repo(roots_repo, "omega")
    other = tmp_path / "registered_other"
    other.mkdir()
    monkeypatch.setattr(mcp_server.registry, "live_daemon_workspaces", lambda: [other.resolve()])

    class _RootsSession:
        client_params = None

        # The rung issues the raw request (the deprecated wrapper is skipped on purpose).
        async def send_request(self, request: object, result_type: object) -> object:
            return SimpleNamespace(roots=[SimpleNamespace(uri=roots_repo.as_uri())])

    async def body() -> None:
        resolver = mcp_server._make_deferred_resolver({}, cwd=tmp_path / "nowhere")
        resolved = await resolver(_RootsSession())
        assert resolved is not None
        _refresh, repo = resolved
        assert repo == roots_repo.resolve()  # roots won; the registry rung never fired

    anyio.run(body)


def test_autoconnect_daemon_dies_before_first_forward_falls_back(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The most delicate interleaving: warm() auto-connects (registry rung) and turns daemon mode
    on, but the daemon dies before the very first forward — the SAME call must fall back to the
    bounded in-process build and serve, not loop or error."""
    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "sigma")
    monkeypatch.setattr(mcp_server.registry, "live_daemon_workspaces", lambda: [repo.resolve()])
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _r: True)

    def _dead(*_a: object, **_k: object) -> object:
        raise mcp_server.daemon_client.DaemonUnavailableError("died instantly")

    monkeypatch.setattr(mcp_server.daemon_client, "query", _dead)
    no_signal_cwd = tmp_path / "editorish"
    no_signal_cwd.mkdir()

    async def body() -> None:
        resolver = mcp_server._make_deferred_resolver({}, cwd=no_signal_cwd)
        server, _warm, _refresh = build_lazy_server(None, None, resolver=resolver)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool(
                    "find_symbol", {"qualified_name": "proj.pkg.a.sigma"}
                )).content[0].text
            )
            assert res["found"] is True  # served by the in-process fallback in the same call

    anyio.run(body)


def test_workspace_root_never_retargets_a_pinned_workspace(tmp_path: Path) -> None:
    """An operator pin (CARTOGATE_REPO / arg at startup) is the strongest signal — the param must
    not move it (only auto-derived workspaces are re-targetable)."""
    pinned = tmp_path / "pinned"
    _tiny_git_repo(pinned, "iota")
    other = tmp_path / "other"
    _tiny_git_repo(other, "omic")

    async def body() -> None:
        refresh = GitLazyRefresh(pinned, repo_id="pinned", resolve=True, index_docs=False)
        server, _warm, _refresh = build_lazy_server(refresh, pinned)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool(
                    "find_symbol",
                    {"qualified_name": "pinned.pkg.a.iota", "workspace_root": str(other)},
                )).content[0].text
            )
            assert res["found"] is True  # still served from the PINNED repo

    anyio.run(body)


def test_workspace_root_retargets_an_autoconnected_workspace(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The wrong-anchor trap (seen in the field): auto-connect picked repo A because it had the
    only live daemon, but THIS window is repo B. The agent naming B via workspace_root must
    re-target — auto-derived workspaces are not sticky. (Agent-chosen ones are: see
    test_workspace_root_param_is_ignored_once_resolved.)"""
    wrong = tmp_path / "wrong_repo"
    wrong.mkdir()
    right = tmp_path / "right_repo"
    _tiny_git_repo(right, "kappa")
    monkeypatch.setattr(mcp_server.registry, "live_daemon_workspaces", lambda: [wrong.resolve()])
    monkeypatch.setattr(
        mcp_server, "_resolved_daemon_ready", lambda r: r == wrong.resolve()
    )

    def _wrong_repo_answer(_repo: Path, name: str, _args: dict, **_k: object) -> dict:
        # Conforms to find_cycles' output_schema — the client validates stubbed answers too.
        return {"cycles": [], "count": 0, "truncated": False, "served_by": "wrong-daemon",
                "tool": name}

    monkeypatch.setattr(mcp_server.daemon_client, "query", _wrong_repo_answer)
    no_signal_cwd = tmp_path / "editorish"
    no_signal_cwd.mkdir()

    async def body() -> None:
        resolver = mcp_server._make_deferred_resolver({}, cwd=no_signal_cwd)
        server, _warm, _refresh = build_lazy_server(None, None, resolver=resolver)
        async with create_connected_server_and_client_session(server) as client:
            # First call, no param: auto-connects to the WRONG repo's daemon.
            first = json.loads(
                (await client.call_tool("find_cycles", {})).content[0].text
            )
            assert first.get("served_by") == "wrong-daemon"
            # The agent names ITS root -> re-targeted and served from the right repo, in-process.
            fixed = json.loads(
                (await client.call_tool(
                    "find_symbol",
                    {"qualified_name": "right_repo.pkg.a.kappa", "workspace_root": str(right)},
                )).content[0].text
            )
            assert fixed["found"] is True

    anyio.run(body)


def test_unresolved_session_with_multiple_live_daemons_lists_them(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    a, b = tmp_path / "one", tmp_path / "two"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(
        mcp_server.registry, "live_daemon_workspaces", lambda: [a.resolve(), b.resolve()]
    )

    no_signal_cwd = tmp_path / "editorish"
    no_signal_cwd.mkdir()

    async def body() -> None:
        resolver = mcp_server._make_deferred_resolver({}, cwd=no_signal_cwd)
        server, _warm, _refresh = build_lazy_server(None, None, resolver=resolver)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool("find_cycles", {})).content[0].text
            )
            assert res["action"] == "set_workspace"
            assert set(res["known_workspaces"]) == {str(a.resolve()), str(b.resolve())}

    anyio.run(body)


def test_set_workspace_registers_the_workspace(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "rho")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("set_workspace", {"root": str(repo)})

    anyio.run(body)
    from cartogate.daemon.registry import registered_workspaces

    assert repo.resolve() in registered_workspaces()  # future sessions can auto-connect


def test_daemon_spawn_carries_no_window_flag(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import subprocess as sp

    seen: dict[str, object] = {}

    class _FakeProc:
        pid = 1

    def _capture(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sp, "Popen", _capture)
    mcp_server._spawn_resolved_daemon(tmp_path)
    assert seen.get("creationflags") == getattr(sp, "CREATE_NO_WINDOW", 0)


def test_at_mention_resource_read_answers_any_uri(tmp_path: Path) -> None:
    """Windsurf's @cartogate mention issues resources/read for mcp://cartogate — without a read
    handler that surfaced as 'Method not found' to the user. Any URI now returns live status."""

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "tau")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_resources()
            assert [str(r.uri) for r in listed.resources] == ["cartogate://status"]

            # Before a workspace is set: status says so (Windsurf asks for mcp://cartogate).
            res = await client.read_resource("mcp://cartogate")
            status = json.loads(res.contents[0].text)  # type: ignore[union-attr]
            assert status["server"] == "cartogate"
            assert status["version"]  # the build identifies itself (git-derived)
            assert status["workspace"] is None
            assert "set_workspace" in status["mode"]
            assert "find_symbol" in status["tools"]

            # After activation the status reflects the live workspace.
            await client.call_tool("set_workspace", {"root": str(repo)})
            res2 = await client.read_resource("cartogate://status")
            status2 = json.loads(res2.contents[0].text)  # type: ignore[union-attr]
            assert status2["workspace"] == str(repo.resolve())
            assert status2["mode"] in ("in-process", "daemon", "indexing")

    anyio.run(body)


def test_deferred_server_instructs_the_agent_to_set_workspace() -> None:
    # Graceful start: the server tells the agent (at connect) to set_workspace up front, so it
    # doesn't have to fail a call, act, and retry.
    deferred, _w1, _r1 = build_lazy_server(None, None, resolver=None)
    instructions = deferred.create_initialization_options().instructions or ""
    assert "workspace_root" in instructions  # the deterministic first-call param leads
    assert "set_workspace" in instructions  # ...with the explicit tool as the fallback

    # A pinned (eager) server needs no such instruction.
    eager, _w2, _r2 = build_lazy_server(_SlowRefreshStub(), Path("."))
    assert eager.create_initialization_options().instructions is None


class _SlowRefreshStub:
    def prime(self) -> InMemoryStore:
        return InMemoryStore()

    def maybe_refresh(self) -> InMemoryStore | None:
        return None


def test_stale_daemon_is_replaced_on_version_mismatch(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Self-healing upgrades: a daemon running OLDER code than this client is stopped and a fresh
    one spawned — after a package upgrade no manual daemon management is ever needed."""
    import cartogate
    from cartogate.daemon.discovery import DiscoveryInfo, read_discovery, write_discovery

    repo = tmp_path / "proj"
    repo.mkdir()
    write_discovery(
        repo,
        DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(), token="t",
                      repo=str(repo), resolve=True),
    )
    monkeypatch.setattr(
        mcp_server.daemon_client, "health", lambda _r, **_k: {"version": "0.0.1+ancient"}
    )
    stopped: list[int] = []
    monkeypatch.setattr(
        mcp_server, "_stop_daemon_process",
        lambda pid, r: (stopped.append(pid), mcp_server.remove_discovery(r)),
    )
    spawned: list[Path] = []
    monkeypatch.setattr(mcp_server, "_spawn_resolved_daemon", lambda r: spawned.append(r))

    assert mcp_server._resolved_daemon_ready(repo) is False  # stale -> not forwardable
    assert stopped and spawned == [repo]  # ...and it was replaced
    # No thrash: the discovery is gone, so a second check bails early — no second stop/spawn.
    assert mcp_server._resolved_daemon_ready(repo) is False
    assert len(stopped) == 1 and spawned == [repo]

    # Same version -> ready (fresh discovery, matching health).
    write_discovery(
        repo,
        DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(), token="t",
                      repo=str(repo), resolve=True),
    )
    monkeypatch.setattr(
        mcp_server.daemon_client,
        "health",
        lambda _r, **_k: {"version": cartogate.__version__, "surface": SURFACE_VERSION},
    )
    assert mcp_server._resolved_daemon_ready(repo) is True
    assert read_discovery(repo) is not None


def test_set_workspace_prefers_a_warm_daemon(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "proj"
    repo.mkdir()
    # Pretend a resolved daemon for this repo is up (started by `cartogate init`).
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _root: True)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert res["ok"] is True and res["mode"] == "daemon"  # forwarded, not indexed

    anyio.run(body)


def test_set_workspace_daemon_loss_falls_back_in_process(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # set_workspace takes the daemon branch, then every daemon query fails -> the session must serve
    # in-process (priming the refresher set_workspace stored), not get stuck.
    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "gamma")
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _r: True)

    def _boom(*_a: object, **_k: object) -> object:
        raise mcp_server.daemon_client.DaemonUnavailableError("daemon gone")

    monkeypatch.setattr(mcp_server.daemon_client, "query", _boom)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            act = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert act["mode"] == "daemon"  # activated against the (mocked) warm daemon
            found = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "proj.pkg.a.gamma"}))
                .content[0].text
            )
            assert found["found"] is True  # daemon query failed -> served in-process, not stuck

    anyio.run(body)


def test_first_call_with_workspace_root_param_self_configures(tmp_path: Path) -> None:
    """The deterministic path: the agent's FIRST tool call carries workspace_root — the server
    activates that workspace and serves the call in one round trip (no failed call first)."""
    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "delta")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            # Every lazy-server tool advertises the optional workspace_root param.
            listed = await client.list_tools()
            by_name = {t.name: t for t in listed.tools}
            assert "workspace_root" in by_name["find_symbol"].input_schema["properties"]
            assert "workspace_root" in by_name["check_duplicate"].input_schema["properties"]

            # One call: configure + serve.
            found = json.loads(
                (await client.call_tool(
                    "find_symbol",
                    {"qualified_name": "proj.pkg.a.delta", "workspace_root": str(repo)},
                )).content[0].text
            )
            assert found["found"] is True

            # A follow-up call needs no param (the workspace is set for the session).
            again = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "proj.pkg.a.delta"}))
                .content[0].text
            )
            assert again["found"] is True

    anyio.run(body)


def test_workspace_root_param_is_ignored_once_resolved(tmp_path: Path) -> None:
    repo1, repo2 = tmp_path / "one", tmp_path / "two"
    _tiny_git_repo(repo1, "alpha")
    _tiny_git_repo(repo2, "beta")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("set_workspace", {"root": str(repo1)})
            # workspace_root on a later call must NOT silently re-target (set_workspace does that).
            res = json.loads(
                (await client.call_tool(
                    "find_symbol",
                    {"qualified_name": "one.pkg.a.alpha", "workspace_root": str(repo2)},
                )).content[0].text
            )
            assert res["found"] is True  # still served from repo1

    anyio.run(body)


def test_workspace_root_param_activates_daemon_and_strips_before_forwarding(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Param + warm daemon: activation prefers the daemon, and the forwarded call must NOT carry
    workspace_root (the daemon's dispatch doesn't know that param)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _r: True)
    seen: list[dict[str, object]] = []

    def _fake_query(_repo: Path, name: str, arguments: dict[str, object], **_k: object) -> dict:
        seen.append(dict(arguments))
        # Conforms to find_symbol's output_schema — the client validates stubbed answers too.
        return {"found": False, "qualified_name": "x.y", "tool": name}

    monkeypatch.setattr(mcp_server.daemon_client, "query", _fake_query)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool(
                    "find_symbol",
                    {"qualified_name": "x.y", "workspace_root": str(repo)},
                )).content[0].text
            )
            assert res["tool"] == "find_symbol"  # served by the (mocked) daemon
            assert seen and "workspace_root" not in seen[0]  # param stripped before forwarding
            assert seen[0] == {"qualified_name": "x.y"}

    anyio.run(body)


def test_workspace_root_param_bad_path_returns_actionable_error() -> None:
    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool(
                    "find_cycles", {"workspace_root": "/no/such/dir/zzz"}
                )).content[0].text
            )
            assert res["action"] == "set_workspace" and "rejected" in res["error"]

            # A non-string value: the SDK validates it against the advertised schema and rejects
            # the call (is_error) before our handler — and if a lax client got through, the
            # isinstance guard would discard it. Either way: no crash, tool not served.
            res2 = await client.call_tool("find_cycles", {"workspace_root": 42})
            assert res2.is_error is True

    anyio.run(body)


def test_tool_arguments_are_validated_against_the_advertised_schema() -> None:
    """mcp 1.x validated every tools/call against the tool's inputSchema inside the SDK; 2.0
    dropped per-tool validation, so the server does it. Without this a wrong-typed argument
    reaches dispatch, gets discarded by a downstream guard, and the call reports SUCCESS —
    a silently served bad call where there used to be a rejection."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            # Wrong type for a declared property: rejected, and the tool never runs.
            bad = await client.call_tool("find_symbol", {"qualified_name": 42})
            assert bad.is_error is True
            assert "invalid arguments" in bad.content[0].text

            # A missing required property is rejected the same way.
            missing = await client.call_tool("check_duplicate", {})
            assert missing.is_error is True

            # Valid arguments are untouched — the guard only rejects what the schema rejects.
            ok = await client.call_tool(
                "find_symbol", {"qualified_name": "sample_pkg.auth.authenticate"}
            )
            assert ok.is_error is False
            assert json.loads(ok.content[0].text)["found"] is True

    anyio.run(body)


def test_handler_exceptions_become_error_results_not_protocol_errors() -> None:
    """1.x's ``@server.call_tool()`` wrapped every handler in
    ``except Exception -> isError=True``. 2.0's ``add_request_handler`` has no such net, so an
    escaping exception surfaces as a JSON-RPC *protocol* error instead — which reads to the
    agent as a broken SERVER rather than a failed tool, and diverges from the daemon surface,
    which returns a structured error for exactly this input."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            res = await client.call_tool("no_such_tool", {})
            assert res.is_error is True
            assert "unknown tool" in json.loads(res.content[0].text)["error"]

    anyio.run(body)


def test_tools_advertise_titles_and_read_only_annotations() -> None:
    """The annotations tell an agent these calls are free: nothing to undo, nothing to confirm,
    no penalty for calling again. That is load-bearing rather than decorative — a gate the agent
    avoids because it looks expensive or risky is a gate that doesn't fire (STRATEGY.md law 3)."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            assert tools, "no tools advertised"
            for tool in tools:
                assert tool.title, f"{tool.name} has no display title"
                ann = tool.annotations
                assert ann is not None, f"{tool.name} has no annotations"
                if tool.name == "contract_status":
                    continue  # runs user-declared shell checks — see the test below
                assert ann.read_only_hint is True, tool.name
                assert ann.destructive_hint is False, tool.name
                assert ann.idempotent_hint is True, tool.name
                assert ann.open_world_hint is False, tool.name  # local graph only, never network

    anyio.run(body)


def test_every_tool_result_carries_matching_structured_content() -> None:
    """2.x dual emit: every successful result carries ``structured_content`` alongside the JSON
    text block, and the two are the SAME payload — a structured-aware client and a text-only
    client must never see different answers.

    The client is also the adversary here: the SDK REJECTS any success result whose
    ``structured_content`` is missing or fails the advertised ``output_schema``, so each
    ``call_tool`` below doubles as an over-the-wire schema-conformance check."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            for tool in tools:
                assert tool.output_schema is not None, f"{tool.name} advertises no output_schema"
            args = {
                "signature": "def gg_probe():", "symbol": "x", "qualified_name": "x",
                "symbols": [], "test": "x", "diff": "",
                "source": "def gg_probe():\n    return 1\n", "line": 2,
            }
            for tool in tools:
                result = await client.call_tool(tool.name, dict(args))
                assert result.is_error is False, tool.name
                assert result.structured_content == json.loads(result.content[0].text), tool.name

    anyio.run(body)


def test_lazy_server_operational_payloads_satisfy_the_output_schema(tmp_path: Path) -> None:
    """The lazy server can answer a SUCCESSFUL call with a payload that is not the tool's dispatch
    result — "which workspace?" errors, set_workspace activations, indexing progress. Once a tool
    advertises an ``output_schema``, the SDK client validates those payloads against it too, so
    each schema must also admit the operational shapes or every pre-resolution call breaks."""
    import subprocess

    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)

    async def body() -> None:
        async def _never_resolves(_session: object) -> None:
            return None

        server, _warm, _refresh = build_lazy_server(None, None, resolver=_never_resolves)
        async with create_connected_server_and_client_session(server) as client:
            # Unresolved: the "which workspace?" payload is a SUCCESS result — it must carry
            # structured_content and pass the client's schema validation (operational branch).
            res = await client.call_tool("find_cycles", {})
            assert res.is_error is False
            assert res.structured_content == json.loads(res.content[0].text)
            assert res.structured_content["action"] == "set_workspace"

            # set_workspace itself advertises a schema; its activation payload must conform.
            activated = await client.call_tool("set_workspace", {"root": str(repo)})
            assert activated.is_error is False
            assert activated.structured_content == json.loads(activated.content[0].text)
            assert activated.structured_content["ok"] is True

            # And a served tool result on the lazy path dual-emits like the static server.
            found = await client.call_tool("find_symbol", {"qualified_name": "proj.pkg.a.helper"})
            assert found.structured_content == json.loads(found.content[0].text)
            assert found.structured_content["found"] is True

    anyio.run(body)


def test_absurd_tool_name_is_rejected_without_flooding_the_log(caplog) -> None:  # type: ignore[no-untyped-def]
    """The tool name is client-controlled and uncapped by the SDK, and ``dispatch`` embeds it in
    its "unknown tool" error — which the traceback then renders into ``.cartogate/mcp.log``, an
    unrotated file in the user's repo. Truncating at the log call is not enough (the traceback
    reproduces it), so the name is bounded at entry: an agent looping on junk calls must not be
    able to fill the disk."""
    import logging

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            with caplog.at_level(logging.WARNING, logger="cartogate"):
                res = await client.call_tool("N" * 200_000, {})
            assert res.is_error is True
            assert "too long" in res.content[0].text
            emitted = "\n".join(r.getMessage() for r in caplog.records)
            assert "N" * 1000 not in emitted, "the oversized name reached the log"

    anyio.run(body)


def test_contract_status_is_not_advertised_read_only() -> None:
    """``contract_status`` answers by RUNNING the contract's declared ``run:`` checks — arbitrary
    user shell commands — plus a ``git write-tree``. Hosts use ``read_only`` to AUTO-APPROVE tool
    calls without prompting the user, so advertising this one read-only would let an agent be
    made to run whatever a repo's contract declares, unattended. The behaviour this guards is
    pinned in test_mcp_contract_tools.py; this asserts the advertised hints match it."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            by_name = {t.name: t for t in (await client.list_tools()).tools}
            ann = by_name["contract_status"].annotations
            assert ann is not None
            assert ann.read_only_hint is False
            assert ann.destructive_hint is True  # a declared check can be `make clean`
            assert ann.idempotent_hint is False  # re-running has further effects
            assert ann.open_world_hint is True  # a declared check can reach the network

    anyio.run(body)


def test_repeated_workspace_attach_does_not_stack_log_handlers(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """``set_workspace`` is advertised idempotent, so repeating it must have no ADDITIONAL effect.

    Before this guard every call added another FileHandler for the same path: one leaked open fd
    per call, and every later log line written once PER handler — so an agent that re-pointed at
    its workspace three times got triplicated logs and three open files.
    """
    import logging

    monkeypatch.delenv("CARTOGATE_LOG_FILE", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for _ in range(3):
            mcp_server.attach_repo_log_file(tmp_path)
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1, f"stacked {len(added)} handlers for one log path"
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_set_workspace_is_not_advertised_as_read_only() -> None:
    """``set_workspace`` retargets the workspace and triggers an index — the one tool that changes
    server state. Advertising it read-only would be a lie the agent acts on."""

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            by_name = {t.name: t for t in (await client.list_tools()).tools}
            ann = by_name["set_workspace"].annotations
            assert ann is not None
            assert ann.read_only_hint is False
            assert ann.destructive_hint is False  # retargets; deletes nothing
            assert ann.idempotent_hint is True  # same root twice lands in the same state

    anyio.run(body)


def test_server_advertises_its_build_version_in_the_handshake() -> None:
    """The handshake is the one place an agent (or a bug report) can see WHICH build answered.
    The SDK defaults `version` to an empty string, which would silently drop the "an installed
    build always identifies its exact commit" guarantee the rest of the tooling upholds."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            info = client.server_info
            assert info is not None
            assert info.version == cartogate.__version__
            assert info.version, "empty version — the SDK default leaked through"

    anyio.run(body)


def test_static_server_schemas_have_no_workspace_root() -> None:
    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            for tool in listed.tools:
                assert "workspace_root" not in tool.input_schema.get("properties", {})

    anyio.run(body)


def test_slow_first_index_detaches_and_reports_instead_of_hanging(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The hang-then-empty fix: a first index that exceeds the inline bound is detached; the
    activation and subsequent calls return explicit 'indexing' payloads (never blocking past the
    client's timeout), and once the build lands a later call commits and serves it."""
    import threading

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "slowsym")
    monkeypatch.setattr(mcp_server, "_ACTIVATE_WAIT_S", 0.05)  # tiny inline bound
    release = threading.Event()
    real_refresh = mcp_server.GitLazyRefresh

    class _SlowRefresh:
        def __init__(self, root: Path, **kwargs: object) -> None:
            self._inner = real_refresh(root, **kwargs)  # type: ignore[arg-type]

        def prime(self) -> object:
            release.wait(timeout=30)  # a long cold index, until the test releases it
            return self._inner.prime()

        def maybe_refresh(self) -> object:
            return self._inner.maybe_refresh()

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _SlowRefresh)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            # Activation: exceeds the bound -> detached, explicit status (well under any timeout).
            act = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert act["ok"] is True and act["status"] == "indexing"

            # A tool call while building: progress payload, NOT a hang and NOT the unresolved error.
            status = json.loads(
                (await client.call_tool("find_cycles", {})).content[0].text
            )
            assert status["status"] == "indexing" and "elapsed_s" in status

            # The build lands -> the next call commits it and serves for real.
            release.set()
            for _ in range(80):  # poll until the detached thread finishes the real prime
                res = json.loads(
                    (await client.call_tool(
                        "find_symbol", {"qualified_name": "proj.pkg.a.slowsym"}
                    )).content[0].text
                )
                if "status" not in res:
                    assert res["found"] is True
                    break
                await anyio.sleep(0.05)
            else:  # pragma: no cover - failure aid
                raise AssertionError("detached index never committed")

    anyio.run(body)


def test_same_root_set_workspace_while_pending_does_not_double_build(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """An explicit set_workspace re-call for the SAME root while its detached build runs reports
    the in-flight build instead of starting a duplicate."""
    import threading

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "solo")
    monkeypatch.setattr(mcp_server, "_ACTIVATE_WAIT_S", 0.05)
    release = threading.Event()
    real_refresh = mcp_server.GitLazyRefresh
    builds: list[int] = []

    class _CountingSlowRefresh:
        def __init__(self, root: Path, **kwargs: object) -> None:
            builds.append(1)
            self._inner = real_refresh(root, **kwargs)  # type: ignore[arg-type]

        def prime(self) -> object:
            release.wait(timeout=30)
            return self._inner.prime()

        def maybe_refresh(self) -> object:
            return self._inner.maybe_refresh()

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _CountingSlowRefresh)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            first = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert first["ok"] is True and first["status"] == "indexing"
            again = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert again["ok"] is True  # reported/committed, not restarted
            assert len(builds) == 1  # exactly one build despite the re-call
            release.set()

    anyio.run(body)


def test_param_retry_during_pending_index_does_not_double_build(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """An agent retrying with the same workspace_root while the detached build runs must get the
    progress payload (or the committed result) — never kick off a second build."""
    import threading

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "once")
    monkeypatch.setattr(mcp_server, "_ACTIVATE_WAIT_S", 0.05)
    release = threading.Event()
    real_refresh = mcp_server.GitLazyRefresh
    builds: list[int] = []

    class _CountingSlowRefresh:
        def __init__(self, root: Path, **kwargs: object) -> None:
            builds.append(1)
            self._inner = real_refresh(root, **kwargs)  # type: ignore[arg-type]

        def prime(self) -> object:
            release.wait(timeout=30)
            return self._inner.prime()

        def maybe_refresh(self) -> object:
            return self._inner.maybe_refresh()

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _CountingSlowRefresh)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            first = json.loads(
                (await client.call_tool(
                    "find_cycles", {"workspace_root": str(repo)}
                )).content[0].text
            )
            assert first["status"] == "indexing"
            # The retry (agent keeps passing the param): progress payload, NOT a second build.
            retry = json.loads(
                (await client.call_tool(
                    "find_cycles", {"workspace_root": str(repo)}
                )).content[0].text
            )
            assert retry.get("status") == "indexing"
            assert len(builds) == 1  # one refresher constructed — no duplicate build
            release.set()
            for _ in range(80):  # after the build lands, the same retry serves for real
                res = json.loads(
                    (await client.call_tool(
                        "find_cycles", {"workspace_root": str(repo)}
                    )).content[0].text
                )
                if res.get("status") != "indexing":
                    assert "cycles" in res
                    break
                await anyio.sleep(0.05)
            else:  # pragma: no cover - failure aid
                raise AssertionError("detached index never committed")
            assert len(builds) == 1  # still exactly one build across activation + retries

    anyio.run(body)


def test_daemon_loss_with_slow_reprime_reports_instead_of_hanging(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The warm() gap: set_workspace picked the daemon, the daemon dies, and the in-process
    fallback prime is SLOW — the tool call must report indexing progress (bounded), not block
    past the client's timeout, and the message must NOT be the misleading 'unknown workspace'.

    (Concurrent-caller note: warm() itself guards the double-build race — `if state["indexing"]
    is not None: return` under the lock — a two-caller test can't be written deterministically,
    so the guard is pinned by inspection + this end-to-end path.)"""
    import threading

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "epsilon")
    monkeypatch.setattr(mcp_server, "_ACTIVATE_WAIT_S", 0.05)
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda _r: True)

    def _gone(*_a: object, **_k: object) -> object:
        raise mcp_server.daemon_client.DaemonUnavailableError("daemon gone")

    monkeypatch.setattr(mcp_server.daemon_client, "query", _gone)
    release = threading.Event()
    real_refresh = mcp_server.GitLazyRefresh

    class _SlowRefresh:
        def __init__(self, root: Path, **kwargs: object) -> None:
            self._inner = real_refresh(root, **kwargs)  # type: ignore[arg-type]

        def prime(self) -> object:
            release.wait(timeout=30)
            return self._inner.prime()

        def maybe_refresh(self) -> object:
            return self._inner.maybe_refresh()

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _SlowRefresh)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            act = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert act["mode"] == "daemon"
            # Daemon dead -> fallback prime is slow -> an explicit indexing status, fast.
            status = json.loads(
                (await client.call_tool("find_cycles", {})).content[0].text
            )
            assert status.get("status") == "indexing"  # not the "unknown workspace" error
            release.set()
            for _ in range(80):  # once the build lands, the session serves in-process
                res = json.loads(
                    (await client.call_tool(
                        "find_symbol", {"qualified_name": "proj.pkg.a.epsilon"}
                    )).content[0].text
                )
                if res.get("status") != "indexing":
                    assert res["found"] is True
                    break
                await anyio.sleep(0.05)
            else:  # pragma: no cover - failure aid
                raise AssertionError("fallback index never committed")

    anyio.run(body)


def test_failed_detached_index_reports_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A build that raises AFTER the inline bound surfaces as an actionable error payload on the
    next tool call — never as an unhandled protocol fault."""
    import threading

    repo = tmp_path / "proj"
    repo.mkdir()
    monkeypatch.setattr(mcp_server, "_ACTIVATE_WAIT_S", 0.05)
    release = threading.Event()

    class _FailingRefresh:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def prime(self) -> object:
            release.wait(timeout=30)
            raise RuntimeError("index blew up late")

        def maybe_refresh(self) -> object:
            return None

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _FailingRefresh)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            act = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert act["ok"] is True and act["status"] == "indexing"
            release.set()
            for _ in range(80):  # the failure surfaces as an actionable error, not a fault
                res = json.loads(
                    (await client.call_tool("find_cycles", {})).content[0].text
                )
                if res.get("status") != "indexing":
                    assert "failed" in res["error"] and res["action"] == "set_workspace"
                    break
                await anyio.sleep(0.05)
            else:  # pragma: no cover - failure aid
                raise AssertionError("detached failure never reported")

    anyio.run(body)


def test_set_workspace_rejects_a_bad_root() -> None:
    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool("set_workspace", {"root": "/no/such/dir/zzz"}))
                .content[0].text
            )
            assert res["ok"] is False and "directory" in res["error"]

    anyio.run(body)


def test_set_workspace_can_retarget_to_a_different_repo(tmp_path: Path) -> None:
    repo1, repo2 = tmp_path / "one", tmp_path / "two"
    _tiny_git_repo(repo1, "alpha")
    _tiny_git_repo(repo2, "beta")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("set_workspace", {"root": str(repo1)})
            found1 = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "one.pkg.a.alpha"}))
                .content[0].text
            )
            assert found1["found"] is True
            # re-target to repo2 -> beta resolves, alpha no longer does
            re = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo2)})).content[0].text
            )
            assert re["ok"] is True and re["repo_id"] == "two"
            found2 = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "two.pkg.a.beta"}))
                .content[0].text
            )
            assert found2["found"] is True
            gone = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "one.pkg.a.alpha"}))
                .content[0].text
            )
            assert gone["found"] is False  # the old workspace is no longer served

    anyio.run(body)


def test_set_workspace_reports_an_index_failure_cleanly(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "proj"
    repo.mkdir()

    class _Boom:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def prime(self) -> InMemoryStore:
            raise RuntimeError("index blew up")

    monkeypatch.setattr(mcp_server, "GitLazyRefresh", _Boom)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            res = json.loads(
                (await client.call_tool("set_workspace", {"root": str(repo)})).content[0].text
            )
            assert res["ok"] is False and "could not index" in res["error"]  # clean, not a fault

    anyio.run(body)


def test_deferred_server_returns_actionable_error_when_unresolvable() -> None:
    """A deferred server whose workspace can't be resolved must answer tool calls with a clear,
    actionable error payload (not hang, not crash) — driven through the real client round-trip."""

    async def body() -> None:
        async def _never_resolves(_session: object) -> None:
            return None  # simulate: no roots, no workspace env, no valid cwd project

        server, _warm, _refresh_once = build_lazy_server(None, None, resolver=_never_resolves)
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("find_cycles", {})
            payload = json.loads(result.content[0].text)
            assert "CARTOGATE_REPO" in payload["error"]  # tells the user how to fix it

    anyio.run(body)


def test_server_answers_resources_and_prompts() -> None:
    """Cartogate is tools-first, but it must answer `resources/list` (the status resource) and
    `prompts/list` (empty) so a client that probes them (e.g. Windsurf) doesn't warn or error.
    """

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            listed = (await client.list_resources()).resources
            assert [str(r.uri) for r in listed] == ["cartogate://status"]
            # The static server serves the GENERIC status payload (no live workspace state).

            res = await client.read_resource("cartogate://status")
            payload = json.loads(res.contents[0].text)  # type: ignore[union-attr]
            assert payload["server"] == "cartogate" and "find_symbol" in payload["tools"]
            assert (await client.list_prompts()).prompts == []

    anyio.run(body)


def test_lazy_server_lists_tools_without_waiting_for_the_index() -> None:
    """The handshake/list_tools must NOT block on the (possibly minutes-long) initial index — that
    block is exactly what makes a client mark the server dead. A slow refresher proves it: tools
    list while prime() is still blocked, and a tool call only then waits for the build to finish.
    """

    class _SlowRefresh:
        def __init__(self) -> None:
            self.release = anyio.Event()
            self.primed = False

        def prime(self) -> InMemoryStore:
            # Simulate a heavy index: block until the test explicitly releases it.
            anyio.from_thread.run(self.release.wait)
            self.primed = True
            return _store()

        def maybe_refresh(self) -> InMemoryStore | None:
            return None

    async def body() -> None:
        refresh = _SlowRefresh()
        # in-process mode (daemon_ready defaults False) -> repo only matters on the daemon path
        server, _warm, _refresh = build_lazy_server(refresh, Path("."))
        async with create_connected_server_and_client_session(server) as client:
            # list_tools returns immediately even though prime() has never run.
            listed = await client.list_tools()
            assert len(listed.tools) == 18  # the 17 graph tools + the set_workspace control tool
            assert "set_workspace" in {t.name for t in listed.tools}
            assert refresh.primed is False  # the index has NOT been built yet

            # A tool call triggers prime(); release it so the call can complete.
            async def _call() -> None:
                await client.call_tool("check_duplicate", {"signature": "def authenticate(name):"})

            async with anyio.create_task_group() as tg:
                tg.start_soon(_call)
                await anyio.sleep(0.05)  # let the call reach prime()'s block
                refresh.release.set()
        assert refresh.primed is True  # the call did build the graph

    anyio.run(body)


def test_refresh_loop_exits_on_cancel() -> None:
    """The background refresh loop must exit when its scope is cancelled (client disconnect) — its
    `except Exception` must NOT swallow cancellation, or the server would hang on exit forever.
    """

    async def body() -> None:
        calls = {"n": 0}

        async def _slow_refresh_once() -> None:
            calls["n"] += 1
            await anyio.sleep(10)  # cancellation must interrupt this (inside the try/except)

        # If the loop swallowed cancellation, this scope could never close and the test would hang.
        with anyio.move_on_after(0.5) as scope:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_refresh_loop, _slow_refresh_once, 0.01)  # fast poll -> enters fast
                await anyio.sleep(5)  # the loop runs until the outer scope cancels everything
        assert scope.cancelled_caught  # the deadline fired and cancellation unwound cleanly
        assert calls["n"] >= 1  # it actually entered refresh_once (so we tested the try-body path)

    anyio.run(body)


def test_lazy_server_refreshes_in_background_not_on_the_tool_call(tmp_path: Path) -> None:
    """The core responsiveness contract: a tool call serves the CURRENT graph and never triggers a
    (minutes-long, resolved) rebuild. Edits are picked up by the background `refresh_once`, not the
    call — so a query right after an edit is fast-but-slightly-stale, never a multi-minute hang.
    """

    async def body() -> None:
        repo = tmp_path / "repo"  # base = repo.parent, so a file's qname is `repo.pkg.m.*`
        pkg = repo / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

        refresh = GitLazyRefresh(repo, repo_id="t", resolve=False, debounce_s=0.0)
        server, warm, refresh_once = build_lazy_server(refresh, repo)
        await warm()  # prime the initial graph

        async with create_connected_server_and_client_session(server) as client:

            async def found(qname: str) -> bool:
                result = await client.call_tool("find_symbol", {"qualified_name": qname})
                return bool(json.loads(result.content[0].text)["found"])

            assert await found("repo.pkg.m.beta") is False  # not added yet

            (pkg / "m.py").write_text(
                "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
            )
            # A tool call must NOT rebuild — so right after the edit the graph is still stale.
            assert await found("repo.pkg.m.beta") is False
            # The background refresh is what picks the edit up and swaps the graph in.
            await refresh_once()
            assert await found("repo.pkg.m.beta") is True

    anyio.run(body)


def test_server_refreshes_tools_when_files_change(tmp_path: Path) -> None:
    """With a refresher, the rich tools reflect edits made *after* startup, not a stale snapshot."""

    async def body() -> None:
        repo = tmp_path / "repo"  # base = repo.parent, so a file's qname is `repo.pkg.m.*`
        pkg = repo / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

        refresh = GitLazyRefresh(repo, repo_id="t", resolve=True, debounce_s=0.0)
        store = refresh.prime()
        server = build_server(store, refresh=refresh)
        async with create_connected_server_and_client_session(server) as client:
            # At startup `beta` does not exist.
            before = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "repo.pkg.m.beta"}))
                .content[0]
                .text
            )
            assert before["found"] is False

            # Edit the file to add `beta`...
            (pkg / "m.py").write_text(
                "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
            )
            # ...the next tool call refreshes and now sees it (no server restart).
            after = json.loads(
                (await client.call_tool("find_symbol", {"qualified_name": "repo.pkg.m.beta"}))
                .content[0]
                .text
            )
            assert after["found"] is True

    anyio.run(body)


def test_no_daemon_requested_detects_arg_and_env() -> None:
    assert _no_daemon_requested(["--no-daemon"], {}) is True
    assert _no_daemon_requested([], {"CARTOGATE_NO_DAEMON": "1"}) is True
    assert _no_daemon_requested([], {"CARTOGATE_NO_DAEMON": "true"}) is True
    assert _no_daemon_requested([], {}) is False
    assert _no_daemon_requested([], {"CARTOGATE_NO_DAEMON": "0"}) is False


def test_daemon_mode_forwards_tool_calls(monkeypatch, tmp_path: Path) -> None:
    # With daemon_ready=True, call_tool forwards to the daemon (no in-process graph built).
    seen: dict[str, object] = {}

    def fake_query(repo, tool, arguments, *, timeout=2.0):  # type: ignore[no-untyped-def]
        seen["tool"] = tool
        seen["timeout"] = timeout
        # Conforms to find_symbol's output_schema — the client validates stubbed answers too.
        return {"found": False, "qualified_name": "x", "forwarded": True, "tool": tool}

    monkeypatch.setattr(daemon_client, "query", fake_query)

    async def body() -> None:
        # refresh.prime would raise if called — proves the daemon path never builds in-process.
        class _Boom:
            def prime(self):  # type: ignore[no-untyped-def]
                raise AssertionError("in-process prime must not run in daemon mode")

            def maybe_refresh(self):  # type: ignore[no-untyped-def]
                return None

        server, _warm, _refresh = build_lazy_server(_Boom(), tmp_path, daemon_ready=True)
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("find_symbol", {"qualified_name": "x"})
            payload = json.loads(result.content[0].text)
            assert payload["forwarded"] is True and payload["tool"] == "find_symbol"
            assert seen["tool"] == "find_symbol"
            assert seen["timeout"] == mcp_server._DAEMON_QUERY_TIMEOUT  # generous, not 2s default

    anyio.run(body)


def test_daemon_loss_falls_back_to_in_process(monkeypatch, tmp_path: Path) -> None:
    # If the daemon goes away, call_tool falls back to the in-process graph for the rest of session.
    def dead_query(repo, tool, arguments, *, timeout=2.0):  # type: ignore[no-untyped-def]
        raise daemon_client.DaemonUnavailableError("daemon died")

    monkeypatch.setattr(daemon_client, "query", dead_query)

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(_store_refresh(), tmp_path, daemon_ready=True)
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(
                "check_duplicate", {"signature": "def authenticate(name):"}
            )
            payload = json.loads(result.content[0].text)
            assert payload["blocked"] is True  # answered in-process after the daemon failed

    anyio.run(body)


def test_decide_daemon_uses_running_daemon(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda repo: True)
    spawned: list[Path] = []
    monkeypatch.setattr(mcp_server, "_spawn_resolved_daemon", lambda repo: spawned.append(repo))
    assert mcp_server._decide_daemon(tmp_path, no_daemon=False) is True
    assert spawned == []  # an existing daemon is reused, not re-spawned


def test_decide_daemon_spawns_when_none_running(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda repo: False)
    spawned: list[Path] = []
    monkeypatch.setattr(mcp_server, "_spawn_resolved_daemon", lambda repo: spawned.append(repo))
    assert mcp_server._decide_daemon(tmp_path, no_daemon=False) is False  # in-process this session
    assert spawned == [tmp_path]  # ...but a daemon is started for next session


def test_decide_daemon_no_daemon_never_spawns(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_resolved_daemon_ready", lambda repo: True)  # even if one's up
    spawned: list[Path] = []
    monkeypatch.setattr(mcp_server, "_spawn_resolved_daemon", lambda repo: spawned.append(repo))
    assert mcp_server._decide_daemon(tmp_path, no_daemon=True) is False
    assert spawned == []  # --no-daemon: never touch the daemon at all


def _store_refresh():  # type: ignore[no-untyped-def]
    class _R:
        def prime(self):  # type: ignore[no-untyped-def]
            return _store()

        def maybe_refresh(self):  # type: ignore[no-untyped-def]
            return None

    return _R()


def test_roots_rung_is_quiet_and_survives_the_deprecated_era(tmp_path: Path) -> None:
    """Stage 3 (SEP-2577): roots/list is deprecated with NO modern replacement — the
    2026-07-28 era forbids server-initiated requests entirely. Cartogate keeps the rung
    as a deliberate legacy-opportunistic signal (stdio only negotiates legacy protocol
    versions, where it works and is the only zero-config per-window signal some editors
    give). Two pins:

    (a) a modern-era session (no back channel) falls through to the next rung quietly;
    (b) the deliberate legacy request emits NO MCPDeprecationWarning — the rung issues
        the raw request, not the SDK's @deprecated wrapper. Under a warnings-as-errors
        regime a wrapper warning would ABORT the call inside the rung's broad except,
        silently losing the roots signal; the repo must still resolve. (Doubles model
        ``send_request`` — the transport seam the rung actually uses; the transport is
        also what raises NoBackChannelError, below the deprecated wrapper.)
    """
    import subprocess
    import warnings

    from mcp.shared.exceptions import MCPDeprecationWarning, NoBackChannelError

    repo = tmp_path / "proj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)

    class _ModernSession:
        async def send_request(self, request: object, result_type: object) -> object:
            raise NoBackChannelError("modern era: no server-initiated requests")

    class _LegacySession:
        async def send_request(self, request: object, result_type: object) -> object:
            assert isinstance(request, types.ListRootsRequest)
            return types.ListRootsResult(roots=[types.Root(uri=repo.resolve().as_uri())])

    async def body() -> None:
        assert await mcp_server._repo_from_client_roots(_ModernSession()) is None
        with warnings.catch_warnings():
            warnings.simplefilter("error", MCPDeprecationWarning)
            resolved = await mcp_server._repo_from_client_roots(_LegacySession())
        assert resolved == repo.resolve()

    anyio.run(body)


def test_the_text_channel_carries_the_same_payload_without_separator_spaces() -> None:
    """Every success is sent twice: as JSON text for text-only hosts and as structured content
    for schema-aware ones. A host that forwards the text pays for each character of it, so the
    text is serialised without the spaces json.dumps puts after ',' and ':' by default."""

    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(
                "find_symbol", {"qualified_name": "sample_pkg.auth.authenticate"}
            )

            text = result.content[0].text
            assert result.is_error is False
            assert json.loads(text) == result.structured_content
            assert text == json.dumps(result.structured_content, separators=(",", ":"))
            assert '": ' not in text and '", "' not in text

    anyio.run(body)


def test_error_results_use_the_compact_text_form_too() -> None:
    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("no_such_tool", {})

            assert result.is_error is True
            text = result.content[0].text
            assert text == json.dumps(json.loads(text), separators=(",", ":"))

    anyio.run(body)


@pytest.mark.parametrize(
    "health",
    [{}, {"surface": None}, {"surface": SURFACE_VERSION - 1}, {"surface": SURFACE_VERSION + 1}],
)
def test_a_daemon_serving_another_result_shape_is_replaced_at_the_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health: dict[str, object]
) -> None:
    """An editable install keeps its install-time version across a `git pull`, so a daemon that
    started before a change to the tool results reports the SAME version as the new server. Its
    old-shape results would be forwarded under the new advertised schemas and rejected by a
    validating client. The daemon therefore reports the result shape it serves, and one that
    reports another (or, like every daemon before this field existed, none) is replaced."""
    import cartogate
    from cartogate.daemon.discovery import DiscoveryInfo, write_discovery

    repo = tmp_path / "proj"
    repo.mkdir()
    write_discovery(
        repo,
        DiscoveryInfo(
            host="127.0.0.1", port=1, pid=os.getpid(), token="t", repo=str(repo), resolve=True
        ),
    )
    monkeypatch.setattr(
        mcp_server.daemon_client,
        "health",
        lambda _r, **_k: {"version": cartogate.__version__, **health},
    )
    stopped: list[int] = []
    monkeypatch.setattr(
        mcp_server,
        "_stop_daemon_process",
        lambda pid, r: (stopped.append(pid), mcp_server.remove_discovery(r)),
    )
    spawned: list[Path] = []
    monkeypatch.setattr(mcp_server, "_spawn_resolved_daemon", lambda r: spawned.append(r))

    assert mcp_server._resolved_daemon_ready(repo) is False
    assert stopped == [os.getpid()] and spawned == [repo]
    # No thrash: the stale daemon's discovery is gone, so asking again replaces nothing.
    assert mcp_server._resolved_daemon_ready(repo) is False
    assert stopped == [os.getpid()] and spawned == [repo]
