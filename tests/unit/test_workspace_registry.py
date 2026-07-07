"""The global workspace registry: how a fresh MCP session finds its repo without any editor signal.

Every activation records the repo in ``~/.cartogate/workspaces.json``; an unresolved session
auto-connects when exactly ONE registered repo has a live resolved daemon. That carries workspace
identity across the editor's constant session restarts — the reliability gap the agent-supplied
root alone can't close (agents forget; sessions reset).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cartogate.daemon.discovery import DiscoveryInfo, write_discovery
from cartogate.daemon.registry import (
    live_daemon_workspaces,
    register_workspace,
    registered_workspaces,
)


def test_register_and_list_roundtrip(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    repo_b = tmp_path / "b"
    repo_b.mkdir()

    register_workspace(repo_a)
    register_workspace(repo_b)
    register_workspace(repo_a)  # re-registering must not duplicate

    listed = registered_workspaces()
    assert listed.count(repo_a.resolve()) == 1
    assert repo_b.resolve() in listed


def test_register_survives_a_corrupt_registry(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    monkeypatch.setenv("CARTOGATE_HOME", str(home))
    (home / ".cartogate").mkdir(parents=True)
    (home / ".cartogate" / "workspaces.json").write_text("not json ][", encoding="utf-8")
    repo = tmp_path / "proj"
    repo.mkdir()

    register_workspace(repo)  # must not raise; rewrites cleanly
    assert repo.resolve() in registered_workspaces()


def test_registry_caps_entries_by_recency(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    repos = []
    for i in range(25):
        repo = tmp_path / f"r{i:02d}"
        repo.mkdir()
        repos.append(repo)
        register_workspace(repo)
    listed = registered_workspaces()
    assert len(listed) <= 20  # bounded — the registry never grows without limit
    assert repos[-1].resolve() in listed  # most recent kept
    assert repos[0].resolve() not in listed  # oldest evicted


def test_live_daemon_workspaces_filters_to_live_resolved_daemons(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    alive, dead, structural = tmp_path / "alive", tmp_path / "dead", tmp_path / "structural"
    for repo in (alive, dead, structural):
        repo.mkdir()
        register_workspace(repo)

    live_pid, dead_pid = os.getpid(), 999_999_999
    write_discovery(alive, DiscoveryInfo(host="127.0.0.1", port=1, pid=live_pid, token="t",
                                         repo=str(alive), resolve=True))
    write_discovery(dead, DiscoveryInfo(host="127.0.0.1", port=1, pid=dead_pid, token="t",
                                        repo=str(dead), resolve=True))
    write_discovery(structural, DiscoveryInfo(host="127.0.0.1", port=1, pid=live_pid, token="t",
                                              repo=str(structural), resolve=False))

    live = live_daemon_workspaces()
    assert live == [alive.resolve()]  # dead pid and structural-only daemons filtered out


def test_registry_drops_vanished_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    gone = tmp_path / "gone"
    gone.mkdir()
    register_workspace(gone)
    gone.rmdir()  # the repo was deleted/moved
    assert gone.resolve() not in registered_workspaces()


def test_registry_file_shape_is_stable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    monkeypatch.setenv("CARTOGATE_HOME", str(home))
    repo = tmp_path / "proj"
    repo.mkdir()
    register_workspace(repo)
    data = json.loads((home / ".cartogate" / "workspaces.json").read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert any(e["path"] == str(repo.resolve()) and "last_active" in e
               for e in data["workspaces"])
