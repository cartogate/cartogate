"""Tests for the sync daemon client, including an end-to-end round-trip against the server."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import anyio
import pytest

from cartogate.daemon import client as daemon_client
from cartogate.daemon.discovery import DiscoveryInfo, write_discovery
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
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_query_without_discovery_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(daemon_client.DaemonUnavailableError):
        daemon_client.query(tmp_path, "check_duplicate", {"signature": "def x():"})


def test_query_with_dead_pid_is_unavailable(tmp_path: Path) -> None:
    write_discovery(
        tmp_path,
        DiscoveryInfo(host="127.0.0.1", port=1, pid=2_000_000_000, token="t", repo=str(tmp_path)),
    )
    with pytest.raises(daemon_client.DaemonUnavailableError):
        daemon_client.query(tmp_path, "check_duplicate", {"signature": "def x():"})


def test_end_to_end_client_against_server(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    async def body() -> None:
        refresh = GitLazyRefresh(repo, repo_id="t", debounce_s=0.0)
        server = DaemonServer(repo, repo_id="t", token="tok", refresh=refresh)
        async with anyio.create_task_group() as tg:
            port: int = await tg.start(server.serve)
            write_discovery(
                repo,
                DiscoveryInfo(
                    host="127.0.0.1", port=port, pid=os.getpid(), token="tok", repo=str(repo)
                ),
            )
            result = await anyio.to_thread.run_sync(
                lambda: daemon_client.query(repo, "check_duplicate", {"signature": "def alpha():"})
            )
            assert result["blocked"] is True
            tg.cancel_scope.cancel()

    anyio.run(body)
