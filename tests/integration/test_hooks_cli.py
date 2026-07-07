"""F-09 auto-refresh: `cartogate hooks install/uninstall` wires the snapshot refresh into git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cartogate.hooks_cli import _HOOKS, cmd_hooks, install_hooks, uninstall_hooks


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    return tmp_path


def test_install_writes_executable_refresh_hooks(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    written = install_hooks(tmp_path)
    assert {p.name for p in written} == set(_HOOKS)
    for hook in written:
        text = hook.read_text(encoding="utf-8")
        assert text.startswith("#!")  # a runnable shell script
        assert "cartogate index" in text  # runs the (incremental) refresh
        assert "cartogate >>>" in text and "cartogate <<<" in text  # marker block
        assert "command -v cartogate" in text  # guarded: a missing binary never breaks a commit
        if os.name != "nt":  # Windows has no Unix exec bit; git runs hooks via sh regardless
            assert hook.stat().st_mode & 0o111  # executable on POSIX


def test_install_is_idempotent(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    install_hooks(tmp_path)
    install_hooks(tmp_path)  # again
    hook = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert hook.count("cartogate >>>") == 1  # not duplicated


def test_install_preserves_an_existing_hook(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    post_commit = tmp_path / ".git" / "hooks" / "post-commit"
    post_commit.write_text("#!/bin/sh\necho existing-hook\n", encoding="utf-8")
    install_hooks(tmp_path)
    text = post_commit.read_text(encoding="utf-8")
    assert "echo existing-hook" in text  # the user's hook is kept
    assert "cartogate index" in text  # ours is appended


def test_uninstall_removes_only_our_block(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    post_commit = tmp_path / ".git" / "hooks" / "post-commit"
    post_commit.write_text("#!/bin/sh\necho existing-hook\n", encoding="utf-8")
    install_hooks(tmp_path)
    uninstall_hooks(tmp_path)
    text = post_commit.read_text(encoding="utf-8")
    assert "echo existing-hook" in text  # preserved
    assert "cartogate" not in text  # our block gone


def test_uninstall_deletes_a_hook_that_was_only_ours(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    install_hooks(tmp_path)
    uninstall_hooks(tmp_path)
    assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()


def test_uninstall_deletes_an_only_ours_hook_with_any_shebang(tmp_path: Path) -> None:
    # A hook that was only our block under a non-/bin/sh shebang is still cleaned up.
    _git_repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/bash\n", encoding="utf-8")
    install_hooks(tmp_path)  # appends our block under the bash shebang
    uninstall_hooks(tmp_path)
    assert not hook.exists()


def test_install_leaves_a_truncated_block_untouched(tmp_path: Path) -> None:
    # A hand-mangled hook with only the BEGIN marker must NOT lose the content after it.
    _git_repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n# >>> cartogate >>>\necho important-user-line\n", encoding="utf-8")
    install_hooks(tmp_path)
    assert "echo important-user-line" in hook.read_text(encoding="utf-8")


def test_cmd_hooks_outside_a_repo_fails_cleanly(tmp_path: Path) -> None:
    assert cmd_hooks(["install", str(tmp_path)]) == 1  # not a git repo -> exit 1, no traceback


def test_cmd_hooks_bad_action_returns_usage(tmp_path: Path) -> None:
    assert cmd_hooks(["frobnicate", str(tmp_path)]) == 2
