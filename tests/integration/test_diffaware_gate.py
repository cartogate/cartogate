"""Diff-aware commit gate — block only what THIS commit introduces (field bug, 2026-07-04).

Field evidence: on a real repo, the first gated commit was refused for ~20 PRE-EXISTING
duplicate groups (React ``Props`` interfaces, per-service ``Settings`` classes) that the staged
change never touched — the exact gate-fatigue failure the strategy's law forbids, and the noise
buried the reference-integrity advisory. The gate must judge THE CHANGE, not the history:
duplicates involving a symbol the staged diff adds or signature-changes BLOCK; everything else
is a one-line pre-existing note. Fail-closed fallback: when git can't answer (non-git dir,
wedged git), every group blocks — the original behavior.
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


def _seed_with_preexisting_duplicate(repo: Path) -> None:
    """History already contains a cross-file duplicate (committed with --no-verify)."""
    _git(repo, "init", "-q")
    (repo / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "history", "--no-verify")


def test_preexisting_duplicates_do_not_block_an_unrelated_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_preexisting_duplicate(tmp_path)
    (tmp_path / "other.py").write_text("def unrelated(q):\n    return q\n", encoding="utf-8")
    _git(tmp_path, "add", "other.py")

    assert precommit_main([str(tmp_path)]) == 0  # the change is clean — it must pass
    err = capsys.readouterr().err
    assert "BLOCKED" not in err
    assert "pre-existing" in err  # ...but the debt is surfaced, quietly
    # A passing diff-aware run still stamps (bypass observability).
    assert (tmp_path / ".cartogate" / "gate_runs.jsonl").exists()


def test_newly_added_duplicate_still_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_preexisting_duplicate(tmp_path)
    (tmp_path / "m3.py").write_text("def sub(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "sub", "--no-verify")
    # The staged change duplicates sub() — introduced by THIS commit.
    (tmp_path / "m4.py").write_text("def sub(a, b):\n    return b - a\n", encoding="utf-8")
    _git(tmp_path, "add", "m4.py")

    assert precommit_main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "BLOCKED:" in err and "sub(a,b)" in err  # the wall prints the normalized key
    assert "add(a,b)" not in err  # the pre-existing group is NOT in the blocking wall
    assert "note: 1 pre-existing" in err  # ...but it IS noted, alongside the wall


def test_signature_change_onto_an_existing_signature_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Editing a symbol's signature so it now collides is introducing a duplicate."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def plus(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "m2.py")

    assert precommit_main([str(tmp_path)]) == 1
    assert "BLOCKED:" in capsys.readouterr().err


def test_initial_commit_duplicates_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On an unborn HEAD everything is introduced by this commit — duplicates block."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 1
    assert "BLOCKED:" in capsys.readouterr().err


def test_rename_smuggled_duplicate_still_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A high-similarity rename is reported as R and would evade the A/M filters (verified
    empirically — a false PASS). Renames are judged as PAIRS: old blob vs new blob, so the
    smuggled signature change counts as new and blocks, while carried-over symbols don't."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "keep.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "old.py").write_text(
        "def plus(a, b):\n    return a + b\n\ndef other(x):\n    return x\n"
        "\ndef third(y):\n    return y * 2\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    # Rename with a high-similarity edit that turns plus() into a duplicate of keep.add().
    _git(tmp_path, "mv", "old.py", "renamed.py")
    (tmp_path / "renamed.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef other(x):\n    return x\n"
        "\ndef third(y):\n    return y * 2\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 1
    assert "BLOCKED:" in capsys.readouterr().err


def test_pure_rename_of_preexisting_duplicate_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pure `git mv` (R100) of a file whose symbols were ALREADY duplicated must not block —
    nothing changed but the path (review finding: --no-renames alone judged every carried-over
    symbol as new). Rename pairs are compared old-blob vs new-blob like modifications."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def helper_one(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "c.py").write_text(
        "def helper_one(x):\n    return x + 1\n\ndef unrelated(y):\n    return y\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "mv", "c.py", "c_moved.py")  # pure rename, zero content change

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "BLOCKED" not in err
    assert "pre-existing" in err


def test_non_git_directory_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without git there is no diff to judge — every duplicate blocks (original behavior)."""
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    assert precommit_main([str(tmp_path)]) == 1
    assert "BLOCKED:" in capsys.readouterr().err
