"""Reference-integrity advisory (STRATEGY.md Phase 1) — the top unserved agent failure mode.

When a staged change ALTERS an established signature, the commit gate prints an ADVISORY
(git shows hook output unconditionally, so the agent always sees it): which contract changed,
the extracted old → new evidence, and the one sanctioned action. Advisory-first per the
gate-fatigue law — it never affects the exit code; promotion to a block requires measured
~0 false positives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cartogate.precommit import main as precommit_main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
        },
    )


def _seed(repo: Path) -> None:
    _git(repo, "init", "-q")
    (repo / "auth.py").write_text("def login(user):\n    return user\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "from auth import login\n\ndef run():\n    return login('a')\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", "--no-verify")


def test_staged_signature_change_prints_the_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user, tenant):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER affects the exit
    err = capsys.readouterr().err
    assert "ADVISORY:" in err
    assert "login" in err
    assert "login(user)" in err and "login(user, tenant)" in err  # old -> new evidence
    assert "ACTION:" in err and "find_references" in err
    assert "BLOCKED" not in err


def test_multiline_signatures_fold_to_one_evidence_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Field transcript (2026-07-05): raw multi-line parameter lists rendered across
    several lines inside the backtick span — fold whitespace like agent_message does."""
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(" + chr(10) + "    user," + chr(10) + "    tenant," + chr(10) + "):"
        + chr(10) + "    return user" + chr(10),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "auth.py")
    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "`login( user, tenant, )`" in err  # folded to one line


def test_body_only_change_prints_no_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text("def login(user):\n    return user * 2\n", encoding="utf-8")
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "ADVISORY:" not in capsys.readouterr().err


def test_default_value_only_change_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """normalize_signature strips defaults: `f(a=1)` -> `f(a=2)` is not a contract break.
    Pinned so a future normalize change can't silently start firing on every default edit."""
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user='guest'):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")
    _git(tmp_path, "commit", "-q", "-m", "defaults", "--no-verify")
    (tmp_path / "auth.py").write_text(
        "def login(user='anon'):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")
    assert precommit_main([str(tmp_path)]) == 0
    assert "ADVISORY:" not in capsys.readouterr().err


def test_annotation_only_change_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user: str):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")
    _git(tmp_path, "commit", "-q", "-m", "annot", "--no-verify")
    (tmp_path / "auth.py").write_text(
        "def login(user: bytes):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")
    assert precommit_main([str(tmp_path)]) == 0
    assert "ADVISORY:" not in capsys.readouterr().err


def test_new_file_prints_no_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    (tmp_path / "extra.py").write_text("def brand_new(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "extra.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "ADVISORY:" not in capsys.readouterr().err


def test_non_git_directory_is_harmless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "solo.py").write_text("def f(a):\n    return a\n", encoding="utf-8")
    assert precommit_main([str(tmp_path)]) == 0  # gate still runs; advisory silently absent
    assert "ADVISORY:" not in capsys.readouterr().err


def test_advisory_rides_along_with_a_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit that both duplicates a symbol AND changes a contract reports the two
    independently: the duplicate blocks, the advisory informs."""
    _seed(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user, tenant):\n    return user\n", encoding="utf-8"
    )
    (tmp_path / "dup.py").write_text("def run():\n    return 1\n", encoding="utf-8")  # dups app.run
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 1  # the duplicate still blocks
    err = capsys.readouterr().err
    assert "BLOCKED:" in err and "ADVISORY:" in err
