"""Name resolution (spec §5.1) — precise binding of a name occurrence to its definition.

The spec specifies LSP for v0 resolution. We use **jedi** directly: jedi is the exact
engine the Python language server (``pylsp``) wraps, so it gives the same off-the-shelf
precision, but in-process and deterministic — no separate stdio language-server process to
supervise (which sidesteps risk R2's warm-process lifecycle and keeps everything air-gapped).
The ``NameResolver`` protocol keeps this swappable: a stack-graphs backend (the strategic
Phase 2 substrate) or a real pylsp client can replace it without touching the pipeline.

Resolution runs at index time only — never on the synchronous gate path — so its latency
is outside the p95 budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import jedi
from jedi.api.environment import InterpreterEnvironment


@dataclass(frozen=True, slots=True)
class Resolved:
    """A resolved definition target."""

    name: str
    full_name: str | None
    def_path: Path | None  # None for builtins / unresolved
    def_line: int | None
    def_type: str  # jedi type: 'function' | 'class' | 'module' | 'param' | 'statement' | ...


class NameResolver(Protocol):
    """Resolves a name occurrence at ``(line, column)`` in a file to its definition."""

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None: ...


class JediResolver:
    """In-process jedi resolver. One project context; scripts cached per file."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        """Args:
        project_root: Directory jedi treats as the import root (the package's parent).
        sources: Map of absolute file path -> source text for every indexed file.
        """
        # Pin jedi to the running interpreter, IN-PROCESS. jedi's default ``SameEnvironment``
        # introspects compiled modules through a ``jedi/inference/compiled/subprocess`` helper that
        # speaks over stdin/stdout; under the MCP stdio server those fds *are* the protocol channel,
        # so that handshake deadlocks and the index hangs at 0% CPU (and it can pick a *different*
        # interpreter than ours — e.g. a system Python vs. our pipx venv). InterpreterEnvironment
        # resolves in-process: no subprocess, faster, and air-gapped/deterministic as intended.
        # The Project carries no ``environment_path``, so its dormant ``get_environment()`` would
        # fall back to jedi's subprocess default — but it is NEVER reached, because every Script
        # below carries the pinned ``environment`` (jedi's InferenceState skips the project lookup
        # when a Script supplies one). GUARD: any future ``jedi.Script`` using ``self._project``
        # MUST also pass ``environment=`` or the deadlock returns.
        environment = InterpreterEnvironment()
        self._project = jedi.Project(path=str(project_root))
        self._scripts: dict[str, jedi.Script] = {
            path: jedi.Script(code=code, path=path, project=self._project, environment=environment)
            for path, code in sources.items()
        }

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        """Resolve the name at ``(line, column)`` to its definition, or ``None``.

        Not thread-safe for *concurrent* calls on one resolver: jedi's ``fast_parser`` (on by
        default) shares a parso module cache, so the resolution pass must call ``resolve`` serially
        per :class:`JediResolver` (the pipeline does — Pass 2 iterates files in a single thread).
        """
        script = self._scripts.get(abs_path)
        if script is None:
            return None
        # follow_imports so a name bound through an import resolves to the real definition.
        definitions = script.goto(line, column, follow_imports=True, follow_builtin_imports=False)
        if not definitions:
            return None
        # jedi may return several candidates for an ambiguous name; v0 takes the first
        # (jedi orders the most specific binding first). Ranking multiple candidates is a
        # future refinement and would be logged at DEBUG.
        definition = definitions[0]
        return Resolved(
            name=definition.name or "",
            full_name=definition.full_name,
            def_path=definition.module_path,
            def_line=definition.line,
            def_type=definition.type or "",
        )
