"""Daemon discovery file: ``<repo>/.cartogate/daemon.json``.

The server writes its host/port/pid/token here on startup; the client reads it to find and
authenticate to the daemon. The token gates access (a local socket is reachable by any local
process), and the file is written owner-only (mode 600 on POSIX; best-effort on Windows). A
dead pid or unreadable file means "no daemon" — the client then falls back to in-process work.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

#: Directory (under the repo) holding daemon state. Should be gitignored.
STATE_DIR = ".cartogate"
_DISCOVERY_NAME = "daemon.json"
_LOG_NAME = "daemon.log"


def ensure_state_dir(repo: Path) -> Path:
    """Create ``<repo>/.cartogate`` as a SELF-IGNORING directory and return it.

    Drops a ``.gitignore`` containing ``*`` inside (the Cargo/Terraform convention), so the runtime
    state (daemon.json, logs, graph snapshot, viz output) never shows in ``git status`` and can't
    be committed by accident — with zero edits to the user's own ``.gitignore``.
    """
    state = repo / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.exists():
        # A read-only tree just means the dir isn't self-ignoring; it still works.
        with contextlib.suppress(OSError):
            ignore.write_text("*\n", encoding="utf-8")
    return state


def log_path(repo: Path) -> Path:
    """Path to the daemon's log file for ``repo`` (where a detached daemon's output lands)."""
    return repo / STATE_DIR / _LOG_NAME


@dataclass(frozen=True, slots=True)
class DiscoveryInfo:
    """Everything a client needs to reach + authenticate to the daemon."""

    host: str
    port: int
    pid: int
    token: str
    repo: str
    #: Whether the daemon holds the *resolved* graph (so it can serve blast_radius / find_references
    #: / etc., not just the structural gate tools). A client wanting a resolved tool uses the daemon
    #: only when this is True. Defaults False so a discovery file from an older daemon reads safely.
    resolve: bool = False


def discovery_path(repo: Path) -> Path:
    """Path to the discovery file for ``repo``."""
    return repo / STATE_DIR / _DISCOVERY_NAME


def write_discovery(repo: Path, info: DiscoveryInfo) -> None:
    """Write the discovery file (owner-only permissions)."""
    path = discovery_path(repo)
    ensure_state_dir(repo)
    path.write_text(json.dumps(asdict(info)), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)  # owner read/write only; a no-op-ish on Windows


def read_discovery(repo: Path) -> DiscoveryInfo | None:
    """Read the discovery file, or ``None`` if missing/unparseable."""
    path = discovery_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DiscoveryInfo(
            host=data["host"],
            port=int(data["port"]),
            pid=int(data["pid"]),
            token=data["token"],
            repo=data["repo"],
            resolve=bool(data.get("resolve", False)),  # absent in an older daemon's file -> False
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def remove_discovery(repo: Path) -> None:
    """Delete the discovery file if present (idempotent)."""
    discovery_path(repo).unlink(missing_ok=True)


def is_pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` currently exists."""
    return bool(psutil.pid_exists(pid))
