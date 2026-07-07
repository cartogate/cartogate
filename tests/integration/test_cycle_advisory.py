"""New-cycle advisory (STRATEGY.md Phase 2) — architecture erosion, judged diff-aware.

Agents erode architecture incrementally ("looks right" imports). The commit gate compares the
module import graph BEFORE and AFTER the staged change (changed files' imports swapped for
their HEAD versions — everything else identical) and reports only cycles THIS change
introduces. Pre-existing cycles are never re-accused; advisory-only, never affects the exit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cartogate.precommit import main as precommit_main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"],
        },
    )


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_staged_change_introducing_a_cycle_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/a.py", "import app.b\n")
    _write(tmp_path, "app/b.py", "x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(tmp_path, "app/b.py", "import app.a\n")  # closes the loop
    _git(tmp_path, "add", "app/b.py")

    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER affects the exit
    err = capsys.readouterr().err
    assert "CYCLE ADVISORY" in err
    assert "app.a -> app.b -> app.a" in err
    assert "ACTION:" in err


def test_preexisting_cycle_is_not_reaccused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/a.py", "import app.b\n")
    _write(tmp_path, "app/b.py", "import app.a\n")  # cycle already in history
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(tmp_path, "other.py", "y = 2\n")
    _git(tmp_path, "add", "other.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "CYCLE ADVISORY" not in capsys.readouterr().err


def test_editing_inside_a_preexisting_cycle_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/a.py", "import app.b\n")
    _write(tmp_path, "app/b.py", "import app.a\nx = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(tmp_path, "app/b.py", "import app.a\nx = 2\n")  # body edit, same imports
    _git(tmp_path, "add", "app/b.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "CYCLE ADVISORY" not in capsys.readouterr().err


def test_new_file_completing_a_cycle_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/a.py", "import app.c\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(tmp_path, "app/c.py", "import app.a\n")  # the new file closes it
    _git(tmp_path, "add", "app/c.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "app.a -> app.c -> app.a" in capsys.readouterr().err


def test_unstaged_worktree_edit_cannot_fabricate_a_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review HIGH-1 (reproduced by the reviewer): an UNSTAGED edit is not what
    `git commit` records — the advisory reads the INDEX for dirty files, so the
    fabricated cycle never appears."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "app/a.py", "x = 1" + chr(10))
    _write(tmp_path, "app/b.py", "x = 1" + chr(10))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(tmp_path, "app/b.py", "import app.a" + chr(10))
    _git(tmp_path, "add", "app/b.py")  # staged half of the would-be cycle
    _write(tmp_path, "app/a.py", "import app.b" + chr(10))  # UNSTAGED other half

    assert precommit_main([str(tmp_path)]) == 0
    assert "CYCLE ADVISORY" not in capsys.readouterr().err


def test_non_git_directory_is_harmless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")
    assert precommit_main([str(tmp_path)]) == 0
    assert "CYCLE ADVISORY" not in capsys.readouterr().err
