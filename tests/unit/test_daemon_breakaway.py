"""The Windows daemon spawn must break away from the client's Job Object so it survives teardown.

Editors (Windsurf / VS Code) run the MCP server inside a kill-on-close Job Object; a plain detached
child dies with the session and can never be warm next time. ``CREATE_BREAKAWAY_FROM_JOB`` escapes
that job — with a fallback for jobs that forbid it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cartogate.daemon.cli import _windows_detached_popen

_DETACHED = 0x8
_NEW_GROUP = 0x200
_BREAKAWAY = 0x0100_0000


@pytest.fixture(autouse=True)
def _pin_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the Windows creationflag constants so the test is deterministic on any platform.
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", _DETACHED, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _NEW_GROUP, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", _BREAKAWAY, raising=False)


def test_requests_job_breakaway(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def _popen(*_args: object, creationflags: int = 0, **_kw: object) -> object:
        seen.append(creationflags)
        return object()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    _windows_detached_popen(["cartogate-daemon"], None)
    assert len(seen) == 1
    assert seen[0] & _BREAKAWAY  # breaks away from the editor's job
    assert seen[0] & _DETACHED and seen[0] & _NEW_GROUP  # still detached + own group


def test_retries_without_breakaway_when_the_job_forbids_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def _popen(*_args: object, creationflags: int = 0, **_kw: object) -> object:
        seen.append(creationflags)
        if creationflags & _BREAKAWAY:
            raise OSError("access denied: job forbids breakaway")
        return object()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    _windows_detached_popen(["cartogate-daemon"], None)  # must not raise — falls back
    assert len(seen) == 2
    assert seen[0] & _BREAKAWAY  # first attempt requested breakaway
    assert not (seen[1] & _BREAKAWAY)  # retry dropped it
    assert seen[1] & _DETACHED and seen[1] & _NEW_GROUP  # but stays detached


def test_daemon_python_prefers_pythonw_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pythonw.exe (GUI subsystem) can never create a console window — the daemon must use it when
    it exists next to the interpreter, and fall back to sys.executable when it doesn't."""
    from cartogate.daemon import cli as daemon_cli

    fake_python = tmp_path / "Scripts" / "python.exe"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_bytes(b"")
    monkeypatch.setattr(daemon_cli.sys, "executable", str(fake_python))

    # (platform injected — patching the real os.name makes pathlib instantiate the wrong Path class)
    assert daemon_cli.daemon_python("nt") == str(fake_python)  # no pythonw yet -> fallback

    (tmp_path / "Scripts" / "pythonw.exe").write_bytes(b"")
    assert daemon_cli.daemon_python("nt").endswith("pythonw.exe")  # present -> preferred

    assert daemon_cli.daemon_python("posix") == str(fake_python)  # POSIX -> always sys.executable


def test_daemon_stop_all_stops_every_registered_live_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`daemon stop --all` — the one-command pre-upgrade step: every registry-known live daemon is
    terminated and its discovery removed (running daemons lock the venv against reinstall)."""
    import os

    from cartogate.daemon import cli as daemon_cli
    from cartogate.daemon.discovery import DiscoveryInfo, read_discovery, write_discovery
    from cartogate.daemon.registry import register_workspace

    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    a, b = tmp_path / "a", tmp_path / "b"
    for repo in (a, b):
        repo.mkdir()
        register_workspace(repo)
        write_discovery(repo, DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(),
                                            token="t", repo=str(repo), resolve=True))
    terminated: list[int] = []

    class _Proc:
        def __init__(self, pid: int) -> None:
            self._pid = pid

        def name(self) -> str:
            return "pythonw.exe"

        def terminate(self) -> None:
            terminated.append(self._pid)

    monkeypatch.setattr(daemon_cli.psutil, "Process", _Proc)
    assert daemon_cli.cmd_stop_all() == 0
    assert len(terminated) == 2  # both live daemons stopped
    assert read_discovery(a) is None and read_discovery(b) is None  # discovery cleared

    assert daemon_cli.cmd_stop_all() == 0  # idempotent: nothing left, still exits 0


def test_stop_never_terminates_a_reused_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale discovery can point at a pid the OS REUSED for an unrelated process — stop must
    refuse to kill anything that isn't a Python interpreter, and still clear the discovery."""
    import os

    from cartogate.daemon import cli as daemon_cli
    from cartogate.daemon.discovery import DiscoveryInfo, read_discovery, write_discovery
    from cartogate.daemon.registry import register_workspace

    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    register_workspace(repo)
    write_discovery(repo, DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(),
                                        token="t", repo=str(repo), resolve=True))
    terminated: list[int] = []

    class _Foreign:
        def __init__(self, pid: int) -> None:
            self._pid = pid

        def name(self) -> str:
            return "chrome.exe"  # the pid was reused by something else entirely

        def terminate(self) -> None:
            terminated.append(self._pid)

    monkeypatch.setattr(daemon_cli.psutil, "Process", _Foreign)
    assert daemon_cli.cmd_stop_all() == 0
    assert terminated == []  # NOT killed
    assert read_discovery(repo) is None  # but the stale discovery is still cleaned up
