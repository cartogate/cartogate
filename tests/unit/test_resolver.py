"""The Python jedi resolver must run fully IN-PROCESS — never spawn a helper subprocess.

Regression for the MCP hang: jedi's default environment discovers a system Python and spawns
``jedi/inference/compiled/subprocess/__main__.py`` that speaks over stdin/stdout. Under the MCP
stdio server those fds *are* the protocol channel, so the handshake deadlocks and the index hangs at
0% CPU forever. Pinning ``InterpreterEnvironment`` keeps resolution in-process (also faster, and
air-gapped/deterministic as the module intends).
"""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver import JediResolver


def _two_file_project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    a = tmp_path / "a.py"
    a.write_text("def helper():\n    return 1\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("from a import helper\n\nx = helper()\n", encoding="utf-8")
    sources = {str(a): a.read_text(encoding="utf-8"), str(b): b.read_text(encoding="utf-8")}
    return tmp_path, sources


def test_resolver_resolves_a_cross_file_name(tmp_path: Path) -> None:
    root, sources = _two_file_project(tmp_path)
    resolver = JediResolver(root, sources)
    # `helper` in `x = helper()` (b.py line 3, col 4) -> the def in a.py line 1.
    resolved = resolver.resolve(str(root / "b.py"), 3, 4)
    assert resolved is not None
    assert resolved.name == "helper"
    assert resolved.def_line == 1
    assert resolved.def_path == root / "a.py"


def test_resolver_pins_the_in_process_interpreter_environment(tmp_path: Path) -> None:
    """Every jedi Script must use ``InterpreterEnvironment`` (in-process), not jedi's default
    ``SameEnvironment`` — which introspects compiled modules via a stdin/stdout subprocess that
    deadlocks under the MCP stdio server (the index hangs at 0% CPU). Asserting the environment type
    is machine-independent: the default would spawn the subprocess only on some hosts (e.g. a pipx
    venv whose interpreter differs from the discovered system Python), so we pin it unconditionally.
    """
    root, sources = _two_file_project(tmp_path)
    resolver = JediResolver(root, sources)
    # ``_inference_state.environment`` is jedi-internal (no public accessor for a Script's
    # environment); verified against jedi 1.x — if jedi renames it this fails loudly, which is the
    # signal to re-check the pin still holds.
    environments = {
        type(script._inference_state.environment).__name__
        for script in resolver._scripts.values()
    }
    assert environments == {"InterpreterEnvironment"}, environments


def test_unresolvable_name_returns_none(tmp_path: Path) -> None:
    """The ``None`` branch: a position with nothing to resolve yields ``None`` (not an error)."""
    root, sources = _two_file_project(tmp_path)
    resolver = JediResolver(root, sources)
    assert resolver.resolve(str(root / "b.py"), 2, 0) is None  # blank line — no name to bind
    assert resolver.resolve(str(root / "missing.py"), 1, 0) is None  # file not in the project
