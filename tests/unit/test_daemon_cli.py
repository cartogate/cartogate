"""Tests for the daemon CLI commands (status/stop routing + discovery cleanup)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cartogate.daemon import cli
from cartogate.daemon.discovery import DiscoveryInfo, read_discovery, write_discovery


def _discovery(repo: Path, pid: int) -> DiscoveryInfo:
    return DiscoveryInfo(host="127.0.0.1", port=1, pid=pid, token="tok", repo=str(repo))


def test_status_not_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_status(tmp_path) == 1
    assert "not running" in capsys.readouterr().out


def test_status_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_discovery(tmp_path, _discovery(tmp_path, os.getpid()))  # our own (alive) pid
    assert cli.cmd_status(tmp_path) == 0
    assert "running" in capsys.readouterr().out


def test_stop_removes_stale_discovery(tmp_path: Path) -> None:
    write_discovery(tmp_path, _discovery(tmp_path, 2_000_000_000))  # dead pid
    assert cli.cmd_stop(tmp_path) == 0
    assert read_discovery(tmp_path) is None


def test_stop_when_not_running(tmp_path: Path) -> None:
    assert cli.cmd_stop(tmp_path) == 1


def test_start_refuses_when_already_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_discovery(tmp_path, _discovery(tmp_path, os.getpid()))  # a live daemon
    assert cli.cmd_start(tmp_path) == 1  # refuses without binding a second one
    assert "already running" in capsys.readouterr().out


def test_main_routes_to_status(tmp_path: Path) -> None:
    assert cli.main(["daemon", "status", str(tmp_path)]) == 1


def test_detached_args_reinvoke_the_daemon_subcommand(tmp_path: Path) -> None:
    # The detached child must re-enter via `... daemon start <root>` (the missing 'daemon'
    # subcommand was a real bug — guard against regressing it).
    args = cli._detached_args(tmp_path)
    assert args[-3:] == ["daemon", "start", str(tmp_path)]


def test_main_resolve_flag_forwards_to_cmd_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_cmd_start(root: Path, *, detach: bool = False, resolve: bool = False) -> int:
        captured["resolve"] = resolve
        captured["detach"] = detach
        return 0

    monkeypatch.setattr(cli, "cmd_start", fake_cmd_start)
    assert cli.main(["daemon", "start", str(tmp_path), "--resolve"]) == 0
    assert captured["resolve"] is True
    # ...and without the flag it's structural.
    assert cli.main(["daemon", "start", str(tmp_path)]) == 0
    assert captured["resolve"] is False
