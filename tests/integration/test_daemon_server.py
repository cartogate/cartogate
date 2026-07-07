"""Integration tests for the daemon TCP server: serve, auth, refresh-on-change."""

from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from cartogate.daemon.protocol import build_request, decode, encode
from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.daemon.server import DaemonServer


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


async def _query(port: int, token: str, tool: str, args: dict) -> dict:
    stream = await anyio.connect_tcp("127.0.0.1", port)
    try:
        await stream.send(encode(build_request(token, tool, args)))
        buffered = BufferedByteReceiveStream(stream)
        line = await buffered.receive_until(b"\n", 1 << 20)
        return decode(line)
    finally:
        await stream.aclose()


def test_queries_never_wait_on_a_slow_refresh(tmp_path: Path) -> None:
    """The stall-to-death regression (user-reported): a resolved rebuild takes minutes, and the
    daemon used to run it INLINE before answering each query — every client timed out. A query must
    now be answered from the current graph immediately, however long the refresh takes."""
    import threading

    release = threading.Event()
    calls: list[int] = []

    class _GlacialRefresh:
        """prime() is instant; maybe_refresh() blocks until the test releases it (and counts)."""

        def __init__(self, inner: GitLazyRefresh) -> None:
            self._inner = inner

        def prime(self) -> object:
            return self._inner.prime()

        def maybe_refresh(self) -> object:
            calls.append(1)
            release.wait(timeout=30)
            return None

    async def body() -> None:
        repo = _make_repo(tmp_path)
        refresh = _GlacialRefresh(GitLazyRefresh(repo, repo_id="t", debounce_s=0.0))
        server = DaemonServer(repo, repo_id="t", token="tok", refresh=refresh)
        async with anyio.create_task_group() as tg:
            port: int = await tg.start(server.serve)
            # Query 1 kicks the (blocked) refresh AND is answered immediately.
            t0 = anyio.current_time()
            first = await _query(port, "tok", "check_duplicate", {"signature": "def alpha():"})
            assert first["ok"] is True and first["result"]["blocked"] is True
            # Queries arriving while the refresh is STILL blocked — served just as fast, and none
            # stacks a second refresh (the in-flight flag).
            second = await _query(port, "tok", "find_symbol", {"qualified_name": "alpha"})
            assert second["ok"] is True
            health = await _query(port, "tok", "__health__", {})  # health must not stall either
            assert health["ok"] is True
            assert anyio.current_time() - t0 < 5  # nowhere near the 30s the refresh would take
            # The kick is start_soon — give the background task a moment to reach maybe_refresh,
            # then pin that N queries during one blocked refresh ran EXACTLY one (no stacking).
            for _ in range(100):
                if calls:
                    break
                await anyio.sleep(0.05)
            assert calls == [1]
            release.set()
            tg.cancel_scope.cancel()

    anyio.run(body)


def test_server_serves_auth_and_refresh(tmp_path: Path) -> None:
    async def body() -> None:
        repo = _make_repo(tmp_path)
        refresh = GitLazyRefresh(repo, repo_id="t", debounce_s=0.0)
        server = DaemonServer(repo, repo_id="t", token="tok", refresh=refresh)
        async with anyio.create_task_group() as tg:
            port: int = await tg.start(server.serve)

            # check_duplicate of an existing function -> blocked.
            ok = await _query(port, "tok", "check_duplicate", {"signature": "def alpha():"})
            assert ok["ok"] is True
            assert ok["result"]["blocked"] is True

            # bad token -> rejected.
            bad = await _query(port, "WRONG", "check_duplicate", {"signature": "def alpha():"})
            assert bad["ok"] is False

            # blast_radius is now a served tool (empty on a structural daemon, not an error).
            edge = await _query(port, "tok", "blast_radius", {"symbol": "x"})
            assert edge["ok"] is True and edge["result"]["found"] is False
            # a genuinely unknown tool -> clear error.
            bad_tool = await _query(port, "tok", "no_such_tool", {})
            assert bad_tool["ok"] is False

            # an oversized newline-less request must NOT crash the daemon.
            with anyio.move_on_after(2):
                try:
                    flood = await anyio.connect_tcp("127.0.0.1", port)
                    await flood.send(b"x" * (1024 * 1024 + 50))
                    await flood.aclose()
                except (anyio.BrokenResourceError, OSError):
                    pass
            # ...the daemon still serves afterwards.
            alive = await _query(port, "tok", "check_duplicate", {"signature": "def alpha():"})
            assert alive["ok"] is True

            # add a new function -> a query KICKS the background refresh and a follow-up sees it
            # (eventually consistent: a query never waits on the rebuild).
            (repo / "pkg" / "m.py").write_text(
                "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
            )
            for _ in range(100):
                fresh = await _query(port, "tok", "check_duplicate", {"signature": "def beta():"})
                assert fresh["ok"] is True  # ALWAYS served, stale or fresh
                if fresh["result"]["blocked"]:
                    break
                await anyio.sleep(0.05)
            else:  # pragma: no cover - failure aid
                raise AssertionError("kicked refresh never landed")

            tg.cancel_scope.cancel()

    anyio.run(body)


def test_server_health_endpoint(tmp_path: Path) -> None:
    async def body() -> None:
        repo = _make_repo(tmp_path)
        refresh = GitLazyRefresh(repo, repo_id="t", debounce_s=0.0)
        server = DaemonServer(repo, repo_id="t", token="tok", refresh=refresh)
        async with anyio.create_task_group() as tg:
            port: int = await tg.start(server.serve)

            resp = await _query(port, "tok", "__health__", {})
            assert resp["ok"] is True
            health = resp["result"]
            assert health["repo_id"] == "t"
            assert health["nodes"] >= 1  # alpha() at least
            assert health["units"] >= 1
            assert health["errors"] == 0
            assert health["last_refresh"]["mode"] == "full"
            assert "uptime_s" in health

            # health still requires auth.
            bad = await _query(port, "WRONG", "__health__", {})
            assert bad["ok"] is False

            tg.cancel_scope.cancel()

    anyio.run(body)


def test_resolved_daemon_serves_resolved_tools(tmp_path: Path) -> None:
    # A daemon started with resolve=True holds the full graph, so it serves blast_radius with real
    # results (the cross-file caller), not just the structural gate tools.
    async def body() -> None:
        repo = tmp_path / "repo"
        pkg = repo / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (pkg / "b.py").write_text(
            "from pkg.a import helper\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

        refresh = GitLazyRefresh(repo, repo_id="t", resolve=True, index_docs=False, debounce_s=0.0)
        server = DaemonServer(repo, repo_id="t", token="tok", refresh=refresh)
        async with anyio.create_task_group() as tg:
            port: int = await tg.start(server.serve)
            resp = await _query(port, "tok", "blast_radius", {"symbol": "repo.pkg.a.helper"})
            assert resp["ok"] is True
            affected = {item["qualified_name"] for item in resp["result"]["affected"]}
            assert "repo.pkg.b.run" in affected  # the resolved caller is found
            tg.cancel_scope.cancel()

    anyio.run(body)
