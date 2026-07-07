"""Tests for the daemon discovery file (.cartogate/daemon.json)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cartogate.daemon.discovery import (
    DiscoveryInfo,
    discovery_path,
    is_pid_alive,
    read_discovery,
    remove_discovery,
    write_discovery,
)


def _info(repo: Path) -> DiscoveryInfo:
    return DiscoveryInfo(
        host="127.0.0.1", port=54321, pid=os.getpid(), token="secret", repo=str(repo)
    )


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    info = _info(tmp_path)
    write_discovery(tmp_path, info)
    assert discovery_path(tmp_path) == tmp_path / ".cartogate" / "daemon.json"
    loaded = read_discovery(tmp_path)
    assert loaded is not None
    assert loaded.port == 54321
    assert loaded.token == "secret"
    assert loaded.pid == os.getpid()
    assert loaded.resolve is False  # default daemon is structural


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_discovery(tmp_path) is None


def test_read_garbage_returns_none(tmp_path: Path) -> None:
    path = discovery_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert read_discovery(tmp_path) is None


def test_remove_discovery(tmp_path: Path) -> None:
    write_discovery(tmp_path, _info(tmp_path))
    remove_discovery(tmp_path)
    assert read_discovery(tmp_path) is None
    remove_discovery(tmp_path)  # idempotent — no error when already gone


def test_is_pid_alive() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(2_000_000_000) is False  # implausible pid


def test_discovery_file_mode_is_owner_only_on_posix(tmp_path: Path) -> None:
    write_discovery(tmp_path, _info(tmp_path))
    if os.name != "nt":  # POSIX permission bits are meaningful
        mode = discovery_path(tmp_path).stat().st_mode & 0o777
        assert mode == 0o600


def test_resolve_flag_round_trips(tmp_path: Path) -> None:
    info = DiscoveryInfo(
        host="127.0.0.1", port=5, pid=1, token="t", repo=str(tmp_path), resolve=True
    )
    write_discovery(tmp_path, info)
    back = read_discovery(tmp_path)
    assert back is not None and back.resolve is True


def test_legacy_file_without_resolve_reads_as_false(tmp_path: Path) -> None:
    (tmp_path / ".cartogate").mkdir()
    (tmp_path / ".cartogate" / "daemon.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": 5, "pid": 1, "token": "t", "repo": str(tmp_path)}),
        encoding="utf-8",
    )
    back = read_discovery(tmp_path)
    assert back is not None and back.resolve is False
