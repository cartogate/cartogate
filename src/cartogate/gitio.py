"""Robust ``git`` invocation for an MCP-hosted process.

Shelling out to git from inside the stdio MCP server is a minefield on Windows, and it cost a long
hunt: capturing git's stdout/stderr through **pipes** means a process that outlives git — a
git-spawned child (fsmonitor daemon, credential helper) or a concurrently-spawned sibling that
inherits the handle — can hold the pipe's write-end open after git exits, so the parent's reader
thread never sees EOF and ``subprocess.communicate()`` blocks **forever**. A ``timeout=`` argument
does NOT save you: the stuck part is the reader-thread join, not the process wait. Separately,
leaving ``stdin`` inherited hands git the server's stdin — which IS the JSON-RPC protocol channel.

:func:`run_git` sidesteps all of it:

* ``stdin=DEVNULL`` — git can never read (or block on) the protocol pipe.
* stdout goes to a temp **file**, not a pipe — there is no reader thread to hang and nothing for a
  stray child to hold open; the parent only ``wait()``s for the process.
* the timeout bounds only that ``wait()``, so it actually fires; on timeout the process is killed
  and we return ``None`` (the caller falls back — e.g. to the non-git directory walk).

This is the single chokepoint for running git; callers must not ``subprocess.run(["git", ...])``
directly.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

#: Windows: run the console-app child (git.exe) WITHOUT creating a console window. Cartogate's
#: long-lived processes (the MCP server Windsurf spawns, the detached daemon) have no console of
#: their own, so every unflagged git spawn would POP UP a terminal window — a visible flash on each
#: refresh poll. 0 on POSIX (the parameter is ignored when zero).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_git(args: list[str], *, cwd: Path, timeout: float) -> bytes | None:
    """Run ``git -C <cwd> <args>`` and return its stdout bytes, or ``None``.

    ``None`` means: git isn't installed, ``cwd`` isn't a git repo (non-zero exit), git timed out, or
    it wedged — every "can't trust the result" case collapses to one signal so callers fall back
    cleanly. Hardened against the Windows pipe-inheritance hang (see the module docstring), and
    spawned window-less (:data:`_NO_WINDOW`) so a console-less parent never flashes a terminal.
    """
    try:
        # A real temp file (not a pipe): the child writes straight to it, so there is no reader
        # thread to deadlock and the timeout only has to bound wait(). TemporaryFile (no visible
        # name, auto-deleted on close) rather than NamedTemporaryFile — subprocess hands the child
        # the handle directly, so no name is needed, and Windows forbids a second open-by-name of a
        # still-open delete-on-close file anyway.
        with tempfile.TemporaryFile() as out:
            proc = subprocess.run(
                ["git", "-C", str(cwd), *args],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode != 0:
                return None
            out.seek(0)
            return out.read()
    except (OSError, subprocess.TimeoutExpired):
        return None
