"""The PreToolUse hook uses a running daemon when one is available (end-to-end)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import anyio

from cartogate.daemon.discovery import DiscoveryInfo, write_discovery
from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.daemon.server import DaemonServer

HOOK_PATH = Path(__file__).resolve().parents[2] / "hooks" / "pretooluse_gate.py"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pretooluse_gate", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_hook_evaluate_blocks_via_daemon(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    hook = _load_hook()

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
            payload = {"tool_input": {"content": "def alpha():\n    return 1\n"}}
            blocked = await anyio.to_thread.run_sync(lambda: hook.evaluate(payload, repo, "t"))
            assert any(v.get("blocked") for v in blocked)
            tg.cancel_scope.cancel()

    anyio.run(body)
