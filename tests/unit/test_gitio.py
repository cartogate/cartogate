"""run_git: the hardened git chokepoint (no pipe-inheritance hang, real timeout, DEVNULL stdin)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cartogate.gitio import run_git


def _init_repo(path: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_returns_stdout_for_a_real_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = run_git(["ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
                  cwd=tmp_path, timeout=10)
    assert out is not None
    assert "a.py" in out.decode("utf-8")


def test_returns_none_outside_a_git_repo(tmp_path: Path) -> None:
    # A non-repo dir -> git exits non-zero -> None (caller falls back), never raises.
    assert run_git(["status", "--porcelain"], cwd=tmp_path, timeout=10) is None


def test_returns_none_on_a_bad_subcommand(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert run_git(["not-a-real-git-command"], cwd=tmp_path, timeout=10) is None


def test_large_output_does_not_deadlock(tmp_path: Path) -> None:
    """Output far bigger than an OS pipe buffer (~64 KB) must round-trip. This is the scenario the
    temp file exists for: a captured PIPE can deadlock on a full buffer, a file cannot."""
    _init_repo(tmp_path)
    # Long names so the ls-files output clears 64 KB without creating a huge number of files.
    name = "a_reasonably_long_module_component_filename_{:05d}_impl.py"  # ~57 chars each
    for i in range(2000):
        (tmp_path / name.format(i)).write_text("x = 1\n", encoding="utf-8")
    out = run_git(["ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
                  cwd=tmp_path, timeout=30)
    assert out is not None
    lines = out.decode("utf-8").splitlines()
    assert len(lines) == 2000
    assert len(out) > 64_000  # confirms we exceeded the pipe-buffer danger zone


def test_stdin_is_devnull_not_inherited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: git's stdin must be DEVNULL, never the caller's (the MCP protocol pipe)."""
    _init_repo(tmp_path)  # before the spy, so only run_git's own call is captured
    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _spy(cmd: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)
    run_git(["status", "--porcelain"], cwd=tmp_path, timeout=10)
    assert captured.get("stdin") is subprocess.DEVNULL
    # stdout must NOT be a pipe (PIPE is what deadlocks); it's a temp file handle.
    assert captured.get("stdout") is not subprocess.PIPE


def test_run_git_spawns_without_a_console_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A console-less parent (the MCP server / detached daemon) must never flash a terminal window
    for its git children — every spawn carries CREATE_NO_WINDOW (0 on POSIX, ignored)."""
    import subprocess as sp

    seen: dict[str, object] = {}
    real_run = sp.run

    def _capture(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sp, "run", _capture)
    run_git(["--version"], cwd=tmp_path, timeout=10)
    assert seen.get("creationflags") == getattr(sp, "CREATE_NO_WINDOW", 0)
