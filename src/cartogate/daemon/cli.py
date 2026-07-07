"""``cartogate daemon start|stop|status`` — manage the warm daemon for a repo/source root."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

import anyio
import psutil

from cartogate.daemon import registry
from cartogate.daemon.discovery import (
    DiscoveryInfo,
    ensure_state_dir,
    is_pid_alive,
    log_path,
    read_discovery,
    remove_discovery,
    write_discovery,
)
from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.daemon.server import DaemonServer

_LOG = logging.getLogger("cartogate")


def cmd_start(
    root: Path, *, repo_id: str | None = None, detach: bool = False, resolve: bool = False
) -> int:
    """Start the daemon (foreground unless ``--detach``), writing the discovery file.

    ``resolve=True`` holds the full *resolved* graph so the daemon can serve every tool
    (blast_radius / find_references / …), not just the structural gate — at the cost of a heavier
    index. The default (structural) daemon stays cheap for the gate.
    """
    root = root.resolve()
    repo_id = repo_id or root.name
    registry.register_workspace(root)  # a manually-started daemon is an auto-connect anchor too
    # Structured logs (refresh failures, the §8.6 trip-wire) go to stderr; for a detached daemon
    # that stderr is redirected to .cartogate/daemon.log, so nothing is lost to the void.
    # (Every code-spawned path redirects stdout/stderr before exec, so sys.stderr is a real handle
    # even under pythonw.exe — only a MANUAL un-redirected pythonw invocation would leave it None,
    # in which case logging degrades silently but the daemon still serves.)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s cartogate %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    existing = read_discovery(root)
    if existing is not None and is_pid_alive(existing.pid):
        print(
            f"cartogate daemon: already running (pid {existing.pid}) "
            f"on {existing.host}:{existing.port}"
        )
        return 1

    if detach:
        return _spawn_detached(root, resolve=resolve)

    token = secrets.token_hex(16)
    refresh = GitLazyRefresh(root, repo_id=repo_id, resolve=resolve, index_docs=resolve)
    server = DaemonServer(root, repo_id=repo_id, token=token, refresh=refresh)

    async def _run() -> None:
        async with anyio.create_task_group() as task_group:
            port: int = await task_group.start(server.serve)
            write_discovery(
                root,
                DiscoveryInfo(
                    host="127.0.0.1", port=port, pid=os.getpid(), token=token, repo=str(root),
                    resolve=resolve,
                ),
            )
            print(f"cartogate daemon: listening on 127.0.0.1:{port} for {root}", flush=True)

    try:
        anyio.run(_run)
    except KeyboardInterrupt:
        pass
    finally:
        remove_discovery(root)
    return 0


def cmd_status(root: Path) -> int:
    """Report whether a daemon is running for ``root``."""
    info = read_discovery(root.resolve())
    if info is not None and is_pid_alive(info.pid):
        print(f"cartogate daemon: running (pid {info.pid}) on {info.host}:{info.port}")
        return 0
    print("cartogate daemon: not running")
    return 1


def _terminate_daemon_pid(pid: int) -> bool:
    """Terminate ``pid`` only if it still looks like a Python daemon — a stale discovery file can
    outlive its daemon, and the OS may have REUSED the pid for an unrelated process; killing that
    would be a nasty surprise. Best-effort: name check then terminate, False on any doubt."""
    try:
        name = psutil.Process(pid).name().lower()
    except psutil.Error:
        return False
    if not name.startswith("python"):  # python.exe / pythonw.exe / python3...
        _LOG.warning("daemon pid %d is now %r (pid reuse) — not terminating it", pid, name)
        return False
    with contextlib.suppress(psutil.Error):
        psutil.Process(pid).terminate()
        return True
    return False


def cmd_stop_all() -> int:
    """Stop every live daemon the workspace registry knows about (``daemon stop --all``).

    The one-command pre-upgrade step: running daemons hold their venv's interpreter open, which
    blocks ``pipx install --force`` on Windows (error 32 on pythonw.exe). This clears all of them
    without hunting processes by name.
    """
    stopped = 0
    for repo in registry.registered_workspaces():
        info = read_discovery(repo)
        if info is None:
            continue
        if is_pid_alive(info.pid) and _terminate_daemon_pid(info.pid):
            print(f"cartogate daemon: stopped {repo} (pid {info.pid})")
            stopped += 1
        remove_discovery(repo)
    if not stopped:
        print("cartogate daemon: none running")
    return 0


def cmd_stop(root: Path) -> int:
    """Stop the daemon for ``root`` and clean up its discovery file."""
    root = root.resolve()
    info = read_discovery(root)
    if info is None:
        print("cartogate daemon: not running")
        return 1
    if is_pid_alive(info.pid):
        _terminate_daemon_pid(info.pid)
    remove_discovery(root)
    print("cartogate daemon: stopped")
    return 0


def daemon_python(platform: str | None = None) -> str:
    """The interpreter to run the daemon with — ``pythonw.exe`` when it exists (Windows).

    A GUI-subsystem interpreter can NEVER create a console window, whatever the spawn flags,
    terminal host, or job-object environment — the deterministic fix for daemon windows flashing
    on some machines even under ``DETACHED_PROCESS``. The daemon's output already goes to
    ``daemon.log`` via explicit handle redirection, which pythonw honors. ``platform`` is a test
    seam (defaults to ``os.name`` — patching the real ``os.name`` breaks pathlib cross-platform).
    """
    if (platform or os.name) == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _detached_args(root: Path, *, resolve: bool = False) -> list[str]:
    """The argv to re-invoke this CLI as a detached ``daemon start`` for ``root``."""
    args = [daemon_python(), "-m", "cartogate.daemon.cli", "daemon", "start", str(root)]
    if resolve:
        args.append("--resolve")
    return args


def _windows_detached_popen(args: list[str], log_fh: IO[Any] | None) -> None:
    """Spawn the daemon on Windows so it OUTLIVES the client that started us.

    ``DETACHED_PROCESS`` + a new process group give it no console and shield it from signals sent to
    the parent. ``CREATE_BREAKAWAY_FROM_JOB`` is the important one: editors like Windsurf / VS Code
    run the MCP server inside a **Job Object with kill-on-close**, so a plain detached child is
    still killed when the editor tears the server down — and the daemon never survives to be warm
    next session. Breaking away escapes that job. A job that forbids breakaway (no
    ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``) makes ``CreateProcess`` fail; we then retry without the flag
    (the daemon starts, but shares the parent's lifetime — no worse than before).
    """
    # getattr avoids static "attr-defined" errors on non-Windows where these are absent.
    base = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(
            args, creationflags=base | breakaway, close_fds=True,
            stdout=log_fh, stderr=subprocess.STDOUT,
        )
    except OSError:
        # A forbidding job raises PermissionError (ERROR_ACCESS_DENIED); we catch OSError broadly on
        # purpose — for a best-effort daemon, falling back to a plain detached spawn on *any*
        # breakaway failure maximizes "the daemon starts". A genuinely broken spawn (impossible
        # here: args[0] is sys.executable) would fail this retry too and propagate — nothing masked.
        subprocess.Popen(
            args, creationflags=base, close_fds=True, stdout=log_fh, stderr=subprocess.STDOUT
        )


def _spawn_detached(root: Path, *, resolve: bool = False) -> int:
    args = _detached_args(root, resolve=resolve)
    # Redirect the detached child's stdout+stderr to a log file so a crash, an index error, or the
    # §8.6 trip-wire is recoverable instead of vanishing — `cartogate doctor` points devs here.
    log = log_path(root)
    ensure_state_dir(root)
    log_fh = open(log, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 (handed to the child)
    try:
        if os.name == "nt":
            _windows_detached_popen(args, log_fh)
        else:
            subprocess.Popen(
                args,
                start_new_session=True,
                close_fds=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
    finally:
        log_fh.close()  # the child holds its own dup'd fd; this parent copy is no longer needed
    print(f"cartogate daemon: spawned for {root} (logs: {log})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cartogate")
    groups = parser.add_subparsers(dest="group", required=True)
    daemon = groups.add_parser("daemon", help="manage the warm gate daemon")
    actions = daemon.add_subparsers(dest="action", required=True)

    start = actions.add_parser("start", help="start the daemon for a repo/source root")
    start.add_argument("root", nargs="?", default=".")
    start.add_argument("--detach", action="store_true", help="run in the background")
    start.add_argument(
        "--resolve", action="store_true",
        help="hold the full resolved graph (serve every tool, not just the structural gate)",
    )
    stop = actions.add_parser("stop", help="stop the daemon for a repo (or --all of them)")
    stop.add_argument("root", nargs="?", default=".")
    stop.add_argument("--all", action="store_true",
                      help="stop every registered daemon (do this before upgrading the package)")
    status = actions.add_parser("status")
    status.add_argument("root", nargs="?", default=".")

    ns = parser.parse_args(argv)
    root = Path(ns.root)
    if ns.action == "start":
        return cmd_start(root, detach=ns.detach, resolve=ns.resolve)
    if ns.action == "stop":
        return cmd_stop_all() if ns.all else cmd_stop(root)
    return cmd_status(root)


if __name__ == "__main__":
    raise SystemExit(main())
