"""Repo discovery for `cartogate-mcp`: a path arg / env, an editor-install guard, else fail loud.

Fixes the DX trap where a client (Windsurf) spawns the server from its own install dir, so the
server silently indexes the *editor's* directory instead of the user's project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.mcp.server import resolve_mcp_repo
from cartogate.surfaces import looks_like_editor_install


@pytest.mark.parametrize(
    "path",
    [
        "C:/Users/me/AppData/Local/Programs/Windsurf",
        "C:/Program Files/Microsoft VS Code",
        "/Applications/Cursor.app/Contents",
        "C:/Users/me/.codeium/windsurf",  # Windsurf reports THIS data dir as its MCP root
        "/home/me/.codeium/windsurf",
    ],
)
def test_editor_install_dirs_are_flagged(path: str) -> None:
    assert looks_like_editor_install(Path(path)) is True


def test_a_dot_codeium_named_project_is_not_flagged(tmp_path: Path) -> None:
    # Only the `.codeium` data dir itself — a project merely named similarly must still resolve.
    assert looks_like_editor_install(Path("C:/proj/.codeium-notes")) is False


def test_a_normal_project_is_not_flagged(tmp_path: Path) -> None:
    assert looks_like_editor_install(tmp_path / "CascadeProjects" / "my-repo") is False


def test_applications_marker_is_root_anchored_not_a_mid_path_dir() -> None:
    # A user's own ``~/applications/`` projects dir must NOT be mistaken for macOS /Applications.
    assert looks_like_editor_install(Path("/home/user/applications/my-repo")) is False
    assert looks_like_editor_install(Path("/Applications/Cursor.app/repo")) is True


def test_explicit_path_argument_wins(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    resolved = resolve_mcp_repo([str(repo)], {}, cwd=tmp_path)
    assert resolved == (repo.resolve(), "proj")


def test_no_daemon_flag_is_not_taken_as_the_path(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    resolved = resolve_mcp_repo(["--no-daemon", str(repo)], {}, cwd=tmp_path)
    assert resolved == (repo.resolve(), "proj")  # the flag is skipped, the path is used


def test_env_var_used_when_no_arg(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    resolved = resolve_mcp_repo([], {"CARTOGATE_REPO": str(repo)}, cwd=tmp_path)
    assert resolved == (repo.resolve(), "proj")


def test_arg_beats_env(tmp_path: Path) -> None:
    (tmp_path / "from_arg").mkdir()
    (tmp_path / "from_env").mkdir()
    resolved = resolve_mcp_repo(
        [str(tmp_path / "from_arg")], {"CARTOGATE_REPO": str(tmp_path / "from_env")}, cwd=tmp_path
    )
    assert resolved is not None and resolved[0].name == "from_arg"


def test_repo_id_override(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    resolved = resolve_mcp_repo([str(repo)], {"CARTOGATE_REPO_ID": "custom"}, cwd=tmp_path)
    assert resolved == (repo.resolve(), "custom")


def test_cwd_in_a_project_resolves(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()  # a project-root marker
    resolved = resolve_mcp_repo([], {}, cwd=tmp_path)
    assert resolved == (tmp_path, tmp_path.name)


def test_cwd_under_an_editor_install_is_refused(tmp_path: Path) -> None:
    editor = tmp_path / "AppData" / "Local" / "Programs" / "Windsurf"
    editor.mkdir(parents=True)
    (editor / ".git").mkdir()  # even with a .git, an editor dir must not be auto-indexed
    assert resolve_mcp_repo([], {}, cwd=editor) is None


def test_cwd_with_no_project_root_is_undetermined(tmp_path: Path) -> None:
    assert resolve_mcp_repo([], {}, cwd=tmp_path) is None  # no marker anywhere -> None (fail loud)
