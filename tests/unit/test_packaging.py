"""Packaging-readiness invariants — distribution metadata is correct and self-consistent.

These guard the things that silently break a `pip install` without touching any source logic:
the version is single-sourced (so `cartogate.__version__`, the installed metadata, and the SCIP
index can never disagree), and the declared console-script entry points stay wired to importable
callables. Deterministic and offline — they read the *installed* metadata of this package.
"""

from __future__ import annotations

import re
import tomllib
from importlib import import_module
from importlib.metadata import entry_points, version
from pathlib import Path

import pytest

import cartogate

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: PEP 440-ish release: at least ``MAJOR.MINOR``, optionally a patch/pre/dev suffix.
#: PEP 440 incl. dev/local segments — git-derived builds (hatch-vcs) look like
#: ``0.1.1.dev3+g7c3e8f4`` or ``0.1.1.dev0+g7c3e8f4ef.d20260703`` and are the NORM for
#: ``pipx install git+...`` users; only the never-versioned placeholders are rejected.
_PEP440 = re.compile(
    r"^\d+\.\d+(\.\d+)?([abc]\d+|\.(post|dev)\d+)*(\+[a-z0-9]+(\.[a-z0-9]+)*)?$"
)

#: The console scripts declared in ``[project.scripts]`` and the targets they must resolve to.
_EXPECTED_SCRIPTS = {
    "cartogate": "cartogate.cli:main",
    # the 5-keystroke convenience alias (cartogate stays canonical - npm's legacy CartoCSS
    # compiler also ships a `carto` binary)
    "carto": "cartogate.cli:main",
    # a thin entry that checks for the optional MCP SDK before importing the server
    "cartogate-mcp": "cartogate.mcp._entry:main",
    # the write-time hard gate (PreToolUse / pre_write_code)
    "cartogate-write-gate": "cartogate.writegate:main",
    # the commit gate as a console script
    "cartogate-precommit": "cartogate.precommit:main",
    # the grep-nudge advisory hook for symbol-shaped grep patterns
    "cartogate-grep-nudge": "cartogate.grepnudge:main",
}


def test_version_is_single_sourced() -> None:
    """The installed distribution version equals ``cartogate.__version__`` (no drift)."""
    assert version("cartogate") == cartogate.__version__


def test_version_is_a_real_release() -> None:
    """The version is valid PEP 440 (dev/local segments included — git-derived builds) and not an
    unversioned placeholder (0.0.0 fallback / uninstalled marker)."""
    assert _PEP440.match(cartogate.__version__), cartogate.__version__
    assert not cartogate.__version__.startswith("0.0.0")


def test_console_scripts_are_declared() -> None:
    """Both console scripts are registered with exactly their declared targets."""
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
    for name, target in _EXPECTED_SCRIPTS.items():
        assert scripts.get(name) == target, f"{name}: {scripts.get(name)!r} != {target!r}"


def test_console_script_targets_are_callable() -> None:
    """Each entry-point target imports to a callable — a registered script that can actually run."""
    for target in _EXPECTED_SCRIPTS.values():
        module_path, _, attr = target.partition(":")
        func = getattr(import_module(module_path), attr)
        assert callable(func), target


def test_core_surfaces_ship_by_default() -> None:
    """The parser + resolver AND the MCP SDK are *base* dependencies, so a bare `pip`/`pipx install`
    gives a working CLI *and* `cartogate-mcp` — no `[extract]`/`[mcp]` extra to remember/forget.
    Read from pyproject — the source of truth."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps = " ".join(data["project"]["dependencies"]).lower()
    for core in ("tree-sitter", "tree-sitter-python", "tree-sitter-typescript", "jedi", "mcp"):
        assert core in deps, f"{core} must be a base dependency (a bare install must work)"
    # the unused python-lsp-server must not bloat the install
    assert "python-lsp-server" not in deps
    # the back-compat extras still exist but are now empty (so `[extract]`/`[mcp]` don't error)
    extras = data["project"]["optional-dependencies"]
    assert extras.get("extract") == [] and extras.get("mcp") == []


@pytest.mark.parametrize("bad", ["abc", "1", "", "v0.1.0", "0.1.0+UPPER", "0.1.0+"])
def test_pep440_regex_rejects_garbage(bad: str) -> None:
    assert not _PEP440.match(bad), bad
