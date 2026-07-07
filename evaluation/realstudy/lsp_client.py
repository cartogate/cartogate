"""A minimal Language Server Protocol client over stdio (stdlib only).

Just enough LSP to drive pyright as an *independent* reference oracle: Content-Length framing,
request/response correlation by id, and auto-replies to the few server→client requests pyright
makes (so it doesn't block). A background thread parses frames into a response map; ``request``
waits on a per-id event with a timeout.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any


class LspClient:
    """Speaks LSP to a subprocess over stdin/stdout."""

    def __init__(self, command: list[str]) -> None:
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._next_id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._events: dict[int, threading.Event] = {}
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # --- public API ----------------------------------------------------------

    def request(self, method: str, params: dict[str, Any], timeout: float = 60.0) -> Any:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            event = threading.Event()
            self._events[rid] = event
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not event.wait(timeout):
            raise TimeoutError(f"LSP request {method} (id={rid}) timed out after {timeout}s")
        with self._lock:
            msg = self._responses.pop(rid, {})
            self._events.pop(rid, None)
        if "error" in msg:
            raise RuntimeError(f"LSP {method} error: {msg['error']}")
        return msg.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def shutdown(self) -> None:
        try:
            self.request("shutdown", {}, timeout=10.0)
            self.notify("exit", {})
        except Exception:
            pass
        self._alive = False
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    # --- framing -------------------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        assert self._proc.stdin is not None
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        while self._alive:
            try:
                header = self._read_header(stream)
            except (EOFError, ValueError):
                break
            length = header.get("Content-Length")
            if length is None:
                break
            body = stream.read(int(length))
            if not body:
                break
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                continue
            self._dispatch(msg)

    @staticmethod
    def _read_header(stream: Any) -> dict[str, str]:
        raw = b""
        while not raw.endswith(b"\r\n\r\n"):
            chunk = stream.read(1)
            if not chunk:
                raise EOFError
            raw += chunk
        headers: dict[str, str] = {}
        for line in raw.decode("ascii").split("\r\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip()] = value.strip()
        return headers

    def _dispatch(self, msg: dict[str, Any]) -> None:
        # A response to one of our requests.
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            rid = msg["id"]
            with self._lock:
                if rid in self._events:
                    self._responses[rid] = msg
                    self._events[rid].set()
            return
        # A server→client request that expects a reply (don't let pyright block on it).
        if "id" in msg and "method" in msg:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            return
        # Otherwise a notification ($/progress, window/logMessage, diagnostics) — ignore.
