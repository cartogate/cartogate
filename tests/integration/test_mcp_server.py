"""Section 4 — the MCP server driven end-to-end via the in-memory client harness.

This is the R6 smoke test: it exercises the real list_tools/call_tool round-trip through
the SDK, so an SDK API change that breaks handler registration fails loudly here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from cartogate.daemon import client as daemon_client
from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.extract.pipeline import index_package
from cartogate.mcp import server as mcp_server
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
                "impact_summary",
                "localize",
                "slice",
            }

            result = await client.call_tool(
                "check_duplicate", {"signature": "def authenticate(name):"}
            )
            assert result.isError is False
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
        return {"found": True, "tool": name}

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
            assert res == {"found": True, "tool": "find_symbol"}  # served via the daemon
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

        async def list_roots(self) -> object:
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
        return {"found": False, "served_by": "wrong-daemon", "tool": name}

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
    from pydantic import AnyUrl

    repo = tmp_path / "proj"
    _tiny_git_repo(repo, "tau")

    async def body() -> None:
        server, _warm, _refresh = build_lazy_server(None, None, resolver=None)
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_resources()
            assert [str(r.uri) for r in listed.resources] == ["cartogate://status"]

            # Before a workspace is set: status says so (Windsurf asks for mcp://cartogate).
            res = await client.read_resource(AnyUrl("mcp://cartogate"))
            status = json.loads(res.contents[0].text)  # type: ignore[union-attr]
            assert status["server"] == "cartogate"
            assert status["version"]  # the build identifies itself (git-derived)
            assert status["workspace"] is None
            assert "set_workspace" in status["mode"]
            assert "find_symbol" in status["tools"]

            # After activation the status reflects the live workspace.
            await client.call_tool("set_workspace", {"root": str(repo)})
            res2 = await client.read_resource(AnyUrl("cartogate://status"))
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
        mcp_server.daemon_client, "health", lambda _r, **_k: {"version": cartogate.__version__}
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
            assert "workspace_root" in by_name["find_symbol"].inputSchema["properties"]
            assert "workspace_root" in by_name["check_duplicate"].inputSchema["properties"]

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
        return {"found": False, "tool": name}

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
            # the call (isError) before our handler — and if a lax client got through, the
            # isinstance guard would discard it. Either way: no crash, tool not served.
            res2 = await client.call_tool("find_cycles", {"workspace_root": 42})
            assert res2.isError is True

    anyio.run(body)


def test_static_server_schemas_have_no_workspace_root() -> None:
    async def body() -> None:
        server = build_server(_store())
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            for tool in listed.tools:
                assert "workspace_root" not in tool.inputSchema.get("properties", {})

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
            from pydantic import AnyUrl

            res = await client.read_resource(AnyUrl("cartogate://status"))
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
            assert len(listed.tools) == 13  # the 12 graph tools + the set_workspace control tool
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
        return {"forwarded": True, "tool": tool}

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
            assert payload == {"forwarded": True, "tool": "find_symbol"}
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
