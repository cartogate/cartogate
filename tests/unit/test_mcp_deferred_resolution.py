"""Deferred workspace discovery: resolve the repo from the client's MCP roots on first tool call.

When the server can't pin the repo from an arg/env/cwd (a client spawns it from its install dir), it
asks the connected client for its workspace via the ``roots`` protocol — a per-window signal, so N
open projects resolve to N correct workspaces. Falls back to workspace env vars, then a guarded cwd.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio

from cartogate.mcp.server import (
    _make_deferred_resolver,
    _repo_from_client_roots,
    _repo_from_workspace_env,
    _uri_to_path,
    _valid_workspace,
    build_lazy_server,
)
from cartogate.store import InMemoryStore


class _FakeSession:
    """Stands in for an MCP ServerSession — only ``list_roots`` / ``client_params`` are used."""

    client_params = None

    def __init__(self, root_uris: list[str] | None = None, *, raises: bool = False) -> None:
        self._uris = root_uris or []
        self._raises = raises

    async def list_roots(self) -> object:
        if self._raises:
            raise RuntimeError("client does not support roots")
        return SimpleNamespace(roots=[SimpleNamespace(uri=u) for u in self._uris])


def test_uri_to_path_handles_file_uris(tmp_path: Path) -> None:
    assert _uri_to_path(tmp_path.as_uri()) == tmp_path
    assert _uri_to_path("https://example.com/x") is None  # non-file scheme


def test_valid_workspace_rejects_missing_and_editor_dirs(tmp_path: Path) -> None:
    assert _valid_workspace(tmp_path) == tmp_path.resolve()
    assert _valid_workspace(tmp_path / "does-not-exist") is None
    editor = tmp_path / "AppData" / "Local" / "Programs" / "Windsurf"
    editor.mkdir(parents=True)
    assert _valid_workspace(editor) is None


def test_roots_picks_the_first_valid_workspace(tmp_path: Path) -> None:
    good = tmp_path / "project"
    good.mkdir()
    session = _FakeSession([good.as_uri()])
    assert anyio.run(_repo_from_client_roots, session) == good.resolve()


def test_roots_skips_an_editor_install_root(tmp_path: Path) -> None:
    editor = tmp_path / "AppData" / "Local" / "Programs" / "Windsurf"
    editor.mkdir(parents=True)
    real = tmp_path / "project"
    real.mkdir()
    session = _FakeSession([editor.as_uri(), real.as_uri()])  # editor first, real second
    assert anyio.run(_repo_from_client_roots, session) == real.resolve()


def test_roots_skips_the_windsurf_codeium_data_dir(tmp_path: Path) -> None:
    # Regression: the newer Windsurf advertises roots but reports its OWN ~/.codeium/windsurf data
    # dir as the workspace. Accepting it made the server index that huge binary tree and hang — it
    # must be refused so resolution falls through to set_workspace.
    data = tmp_path / ".codeium" / "windsurf"
    data.mkdir(parents=True)
    session = _FakeSession([data.as_uri()])
    assert anyio.run(_repo_from_client_roots, session) is None


def test_roots_picks_a_real_project_over_the_codeium_data_dir(tmp_path: Path) -> None:
    # The guard must skip the .codeium data dir but STILL pick a real workspace when one is offered.
    data = tmp_path / ".codeium" / "windsurf"
    data.mkdir(parents=True)
    real = tmp_path / "myproject"
    real.mkdir()
    session = _FakeSession([data.as_uri(), real.as_uri()])  # junk first, real project second
    assert anyio.run(_repo_from_client_roots, session) == real.resolve()


def test_roots_unavailable_returns_none() -> None:
    assert anyio.run(_repo_from_client_roots, _FakeSession(raises=True)) is None
    assert anyio.run(_repo_from_client_roots, _FakeSession([])) is None  # client reports no roots


def test_roots_times_out_on_a_hung_client(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A client that declares roots but never answers must not stall — it times out and falls back.
    monkeypatch.setattr("cartogate.mcp.server._ROOTS_TIMEOUT_S", 0.05)

    class _Hang:
        client_params = None

        async def list_roots(self) -> object:
            await anyio.sleep(5)  # never answers within the (patched) timeout
            return SimpleNamespace(roots=[])

    assert anyio.run(_repo_from_client_roots, _Hang()) is None


def test_deferred_warm_throttles_reresolution_after_a_failure(tmp_path: Path) -> None:
    calls: list[int] = []

    async def resolver(session: object) -> None:
        calls.append(1)
        return None  # always undeterminable

    _server, warm, _refresh_once = build_lazy_server(None, None, resolver=resolver)
    anyio.run(warm, _FakeSession([]))  # 1st call: resolver runs, fails, backs off
    anyio.run(warm, _FakeSession([]))  # 2nd (immediate): throttled — resolver NOT re-run
    assert calls == [1]


def test_repo_from_workspace_env(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _repo_from_workspace_env({"WORKSPACE_FOLDER": str(proj)}) == proj.resolve()
    assert _repo_from_workspace_env({"PROJECT_ROOT": str(proj)}) == proj.resolve()
    assert _repo_from_workspace_env({"WORKSPACE_FOLDER": str(tmp_path / "nope")}) is None
    assert _repo_from_workspace_env({}) is None


def test_deferred_resolver_prefers_roots(tmp_path: Path) -> None:
    proj = tmp_path / "from_roots"
    proj.mkdir()
    resolver = _make_deferred_resolver({}, cwd=tmp_path)
    result = anyio.run(resolver, _FakeSession([proj.as_uri()]))
    assert result is not None
    refresh, repo = result
    assert repo == proj.resolve()


class _SpyRefresh:
    """A refresher whose prime() just records it ran — to test the deferred warm() wiring."""

    last_refresh = None

    def __init__(self) -> None:
        self.primed = False

    def prime(self) -> InMemoryStore:
        self.primed = True
        return InMemoryStore()

    def maybe_refresh(self) -> None:
        return None


def test_deferred_warm_primes_only_once_a_session_arrives(tmp_path: Path) -> None:
    spy = _SpyRefresh()

    async def resolver(session: object) -> tuple[_SpyRefresh, Path]:
        return spy, tmp_path

    _server, warm, _refresh_once = build_lazy_server(None, None, resolver=resolver)
    anyio.run(warm)  # background pre-warm, no session -> must NOT resolve/prime yet
    assert spy.primed is False
    anyio.run(warm, _FakeSession([]))  # a real tool call carries the session -> resolve + prime
    assert spy.primed is True


def test_deferred_resolver_falls_back_to_env_then_none(tmp_path: Path) -> None:
    proj = tmp_path / "from_env"
    proj.mkdir()
    # roots empty -> env used
    resolver = _make_deferred_resolver({"WORKSPACE_FOLDER": str(proj)}, cwd=tmp_path)
    result = anyio.run(resolver, _FakeSession([]))
    assert result is not None and result[1] == proj.resolve()
    # nothing anywhere (cwd has no project marker) -> None (server returns an actionable error)
    empty = _make_deferred_resolver({}, cwd=tmp_path)
    assert anyio.run(empty, _FakeSession([])) is None
