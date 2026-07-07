"""pyright reference oracle (V3) — independent of jedi, so it can grade Cartogate fairly.

Drives ``pyright-langserver`` over LSP: opens every source file in the package, then for a
symbol's definition position asks ``textDocument/references`` (declaration excluded). Returns
the set of files that reference the symbol, normalized relative to the source root so it lines
up with Cartogate's ``unit`` strings and the grep baseline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .lsp_client import LspClient

_DEF_RE = re.compile(r"^(?:\s*)(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")


def pyright_command() -> list[str]:
    """Resolve a command that starts pyright's language server over stdio.

    ``pyright-langserver`` ships as a Node entry point (``langserver.index.js``) with no
    shebang, so it must be run as ``node <path> --stdio``. We locate it (env override, npm
    global root, or the npx cache), priming the npx cache with ``npx -y pyright`` if needed.
    """
    override = os.environ.get("PYRIGHT_LANGSERVER_JS")
    if override:
        return ["node", override, "--stdio"]

    candidates: list[Path] = []
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if root:
            candidates.append(Path(root) / "pyright" / "langserver.index.js")
    except (OSError, subprocess.SubprocessError):
        pass
    home = Path.home()
    candidates += sorted(home.glob(".npm/_npx/*/node_modules/pyright/langserver.index.js"))

    for path in candidates:
        if path.is_file():
            return ["node", str(path), "--stdio"]

    # Prime the npx cache, then look again.
    if shutil.which("npx"):
        subprocess.run(["npx", "-y", "pyright", "--version"], capture_output=True, timeout=300)
        for path in sorted(home.glob(".npm/_npx/*/node_modules/pyright/langserver.index.js")):
            if path.is_file():
                return ["node", str(path), "--stdio"]
    raise RuntimeError(
        "could not locate pyright-langserver; set PYRIGHT_LANGSERVER_JS or `npm i -g pyright`."
    )


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


class PyrightReferences:
    """Context manager exposing ``references(file, line, col) -> set[str]`` over a package."""

    def __init__(self, src_root: Path, package_dir: Path, *, settle: float = 8.0) -> None:
        self.src_root = src_root.resolve()
        self.package_dir = package_dir.resolve()
        self._settle = settle
        self._client: LspClient | None = None
        self._files = sorted(self.package_dir.rglob("*.py"))

    def __enter__(self) -> PyrightReferences:
        client = LspClient(pyright_command())
        self._client = client
        client.request(
            "initialize",
            {
                "processId": None,
                "rootUri": _uri(self.src_root),
                "capabilities": {},
                "workspaceFolders": [{"uri": _uri(self.src_root), "name": "corpus"}],
                "initializationOptions": {},
            },
            timeout=60,
        )
        client.notify("initialized", {})
        # Open every file so pyright has the whole package in memory for cross-file refs.
        for path in self._files:
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": _uri(path),
                        "languageId": "python",
                        "version": 1,
                        "text": path.read_text(encoding="utf-8", errors="replace"),
                    }
                },
            )
        # Give the background analyzer a moment to settle before querying.
        import time

        time.sleep(self._settle)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._client is not None:
            self._client.shutdown()

    def references(self, rel_file: str, def_line: int) -> set[str]:
        """References to the symbol defined at ``rel_file`` line ``def_line`` (1-based).

        ``def_line`` matches Cartogate's ``location.start_line``. Returns files relative to the
        source root (e.g. ``click/core.py``), declaration excluded.
        """
        assert self._client is not None
        abs_file = (self.src_root / rel_file).resolve()
        line0, col0 = self._name_position(abs_file, def_line)
        if line0 is None:
            return set()
        result = self._client.request(
            "textDocument/references",
            {
                "textDocument": {"uri": _uri(abs_file)},
                "position": {"line": line0, "character": col0},
                "context": {"includeDeclaration": False},
            },
            timeout=90,
        )
        out: set[str] = set()
        for loc in result or []:
            uri = loc.get("uri", "")
            if uri.startswith("file://"):
                p = Path(uri[len("file://"):])
                try:
                    out.add(p.resolve().relative_to(self.src_root).as_posix())
                except ValueError:
                    continue
        return out

    @staticmethod
    def _name_position(abs_file: Path, def_line: int) -> tuple[int | None, int]:
        """Return the 0-based (line, char) of the symbol name on its definition line."""
        try:
            lines = abs_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None, 0
        if not (1 <= def_line <= len(lines)):
            return None, 0
        line = lines[def_line - 1]
        match = _DEF_RE.match(line)
        if not match:
            return None, 0
        return def_line - 1, match.start(1)
