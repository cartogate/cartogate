"""Global registry of recently-activated workspaces (``~/.cartogate/workspaces.json``).

The reliability gap this closes: editors like Windsurf give an MCP server ZERO workspace signal,
so the session depends on the agent supplying the root — but sessions restart constantly and
agents forget. The registry carries workspace identity ACROSS sessions: every activation (an
agent's ``set_workspace``/``workspace_root``, an eager pin, ``cartogate init``) records the repo;
a fresh, unresolved session then auto-connects when exactly ONE registered repo has a live
*resolved* daemon (the daemon's uptime spans editor sessions, so it is the durable anchor).
Multiple live daemons → the caller lists them and the agent disambiguates — the only per-window
signal that exists.

Not a lock-guarded database: last-writer-wins on a tiny JSON file is fine for a per-user list of
project paths, writes are atomic (temp + replace), and a corrupt file is simply rewritten.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from cartogate.daemon.discovery import ensure_state_dir, is_pid_alive, read_discovery

_LOG = logging.getLogger("cartogate")

#: Registry cap — recency-evicted. A per-user list of projects; 20 is far beyond real use.
_MAX_ENTRIES = 20

_VERSION = 1


def _registry_path() -> Path:
    home = os.environ.get("CARTOGATE_HOME")
    base = Path(home) if home else Path.home()
    return base / ".cartogate" / "workspaces.json"


def _load() -> list[dict[str, object]]:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("workspaces") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def register_workspace(repo: Path) -> None:
    """Record ``repo`` as an active workspace (idempotent; recency-capped; never raises).

    Called on every activation path — this must never be able to break serving, so any IO error
    is logged and swallowed.
    """
    try:
        resolved = str(repo.resolve())
        entries = [e for e in _load() if e.get("path") != resolved]
        entries.append({"path": resolved, "last_active": time.time()})
        def _stamp(entry: dict[str, object]) -> float:
            value = entry.get("last_active", 0)
            return float(value) if isinstance(value, (int, float)) else 0.0

        entries.sort(key=_stamp)
        entries = entries[-_MAX_ENTRIES:]
        path = _registry_path()
        ensure_state_dir(path.parent.parent)  # ~/.cartogate — consistent with repo state dirs
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"version": _VERSION, "workspaces": entries}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 — a registry problem must never break serving
        _LOG.warning("cartogate: could not update the workspace registry (%s)", exc)


def registered_workspaces() -> list[Path]:
    """Registered workspaces that still exist on disk, most recent last."""
    out: list[Path] = []
    for entry in _load():
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        try:
            if path.is_dir():
                out.append(path.resolve())
        except OSError:
            continue
    return out


def live_daemon_workspaces() -> list[Path]:
    """Registered workspaces with a LIVE *resolved* daemon — the auto-connect candidates.

    Liveness = discovery file + alive pid + ``resolve`` flag (a structural-only daemon can't serve
    the full tool surface, so it can't anchor a session). The caller auto-connects on exactly one.
    """
    live: list[Path] = []
    for repo in registered_workspaces():
        info = read_discovery(repo)
        if info is not None and info.resolve and is_pid_alive(info.pid):
            live.append(repo)
    return live
