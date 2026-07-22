"""Test that cartogate.nav is isolated from core cartogate modules."""

from __future__ import annotations

import subprocess
import sys


def test_nav_isolation_from_precommit() -> None:
    """Importing cartogate.precommit does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.precommit; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_nav_isolation_from_cli() -> None:
    """Importing cartogate.cli does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.cli; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_nav_isolation_from_daemon_cli() -> None:
    """Importing cartogate.daemon.cli does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.daemon.cli; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_nav_isolation_from_extract_pipeline() -> None:
    """Importing cartogate.extract.pipeline does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.extract.pipeline; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_nav_isolation_from_mcp_server() -> None:
    """Importing cartogate.mcp.server does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.mcp.server; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_nav_isolation_from_daemon_server() -> None:
    """Importing cartogate.daemon.server does not import cartogate.nav*."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartogate.daemon.server; "
            "import sys; "
            "nav_mods = [m for m in sys.modules if m.startswith('cartogate.nav')]; "
            "assert not nav_mods, f'Found nav modules: {nav_mods}'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
