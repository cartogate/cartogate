"""The *without-Cartogate* arm's tools: generic filesystem primitives over the corpus.

This is the realistic baseline an agent has with no code-graph: read a file, list a
directory, and grep. All paths are sandboxed under the corpus root so the agent cannot
wander outside the codebase under test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 60_000
MAX_GREP_HITS = 200


def tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-format tool definitions for the baseline arm."""
    return [
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file under the project root. Returns its contents.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to root."}},
                "required": ["path"],
            },
        },
        {
            "name": "list_dir",
            "description": "List the entries of a directory under the project root.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Dir relative to root."}},
                "required": ["path"],
            },
        },
        {
            "name": "grep",
            "description": "Search the project for a regex; returns matching path:line lines.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "default": "*.py", "description": "Filename glob."},
                },
                "required": ["pattern"],
            },
        },
    ]


def make_executor(root: Path):
    """Return ``executor(name, arguments) -> dict`` over the sandboxed corpus ``root``."""
    root = root.resolve()

    def _safe(rel: str) -> Path:
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"path escapes project root: {rel}")
        return target

    def _read_file(path: str) -> dict[str, Any]:
        target = _safe(path)
        data = target.read_text(encoding="utf-8", errors="replace")[:MAX_READ_BYTES]
        return {"path": path, "content": data}

    def _list_dir(path: str) -> dict[str, Any]:
        target = _safe(path)
        entries = sorted(
            (p.name + ("/" if p.is_dir() else "")) for p in target.iterdir()
        )
        return {"path": path, "entries": entries}

    def _grep(pattern: str, glob: str = "*.py") -> dict[str, Any]:
        regex = re.compile(pattern)
        hits: list[str] = []
        for file in sorted(root.rglob(glob)):
            if not file.is_file():
                continue
            try:
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(root).as_posix()}:{i}: {line.strip()}")
                    if len(hits) >= MAX_GREP_HITS:
                        return {"pattern": pattern, "hits": hits, "truncated": True}
        return {"pattern": pattern, "hits": hits, "truncated": False}

    def _execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "read_file":
            return _read_file(arguments["path"])
        if name == "list_dir":
            return _list_dir(arguments["path"])
        if name == "grep":
            return _grep(arguments["pattern"], arguments.get("glob", "*.py"))
        raise ValueError(f"unknown tool: {name!r}")

    return _execute
