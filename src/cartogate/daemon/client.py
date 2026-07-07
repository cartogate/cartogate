"""Synchronous daemon client for the (sync) git hooks.

Uses a plain blocking socket — the hooks are short-lived scripts, not async. Any failure to
reach a healthy daemon (no discovery file, dead pid, refused connection, token rejected, error
response) raises :class:`DaemonUnavailableError`, which the caller treats as "fall back to
indexing in-process". So the daemon is a pure accelerator: the gate works with or without it.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from cartogate.daemon.discovery import is_pid_alive, read_discovery
from cartogate.daemon.protocol import ProtocolError, build_request, decode, encode


class DaemonUnavailableError(Exception):
    """The daemon could not be reached or returned an error; fall back to in-process work.

    ``expected`` distinguishes "no daemon was ever started" (the common, benign case — stay quiet)
    from a daemon that *should* be answering but isn't (a crash / dead pid / refused connection —
    worth telling the developer about). The gate uses this to decide whether to warn.
    """

    def __init__(self, message: str, *, expected: bool = False) -> None:
        super().__init__(message)
        self.expected = expected


def query(
    repo: Path, tool: str, arguments: dict[str, Any], *, timeout: float = 2.0
) -> dict[str, Any]:
    """Query a running daemon for ``repo`` and return the tool result.

    Raises:
        DaemonUnavailableError: if no healthy daemon answers (caller should fall back).
    """
    info = read_discovery(repo)
    if info is None:
        # No discovery file => no daemon was started. Benign: the gate just indexes in-process.
        raise DaemonUnavailableError("no daemon discovery file", expected=True)
    if not is_pid_alive(info.pid):
        raise DaemonUnavailableError("daemon pid is not alive")  # crashed/killed — not expected

    try:
        with socket.create_connection((info.host, info.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(encode(build_request(info.token, tool, arguments)))
            response = decode(_recv_line(sock))
    except (OSError, ProtocolError) as exc:
        raise DaemonUnavailableError(f"daemon request failed: {exc}") from exc

    if not response.get("ok"):
        raise DaemonUnavailableError(str(response.get("error", "daemon error")))
    result = response.get("result")
    if not isinstance(result, dict):
        raise DaemonUnavailableError("malformed daemon result")
    return result


def health(repo: Path, *, timeout: float = 2.0) -> dict[str, Any]:
    """Return the running daemon's health/stats, or raise :class:`DaemonUnavailableError`."""
    from cartogate.daemon.server import HEALTH_TOOL

    return query(repo, HEALTH_TOOL, {}, timeout=timeout)


def _recv_line(sock: socket.socket, max_bytes: int = 1 << 20) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk or total > max_bytes:
            break
    return b"".join(chunks)
