"""Bypass observability (STRATEGY.md Phase 1) — commits that dodged the gate become visible.

Agents demonstrably bypass out-of-session gates (documented ``--no-verify`` evasion). We can't
prevent the flag, but we can make it observable: a PASSING pre-commit run stamps the staged
tree hash (``git write-tree``) into ``.cartogate/gate_runs.jsonl``; ``cartogate stats`` then
reports any recent commit whose tree carries no stamp — a bypass, or a commit made without
the gate. Deterministic: tree-hash equality, no heuristics.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cartogate.precommit import main as precommit_main
from cartogate.stats import gate_coverage
from cartogate.stats import run as stats_run


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"],
        },
    )


def test_passing_gate_stamps_the_staged_tree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    assert precommit_main([str(tmp_path)]) == 0
    stamps = (tmp_path / ".cartogate" / "gate_runs.jsonl").read_text(encoding="utf-8")
    assert '"tree"' in stamps


def test_coverage_separates_verified_from_bypassed(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    assert precommit_main([str(tmp_path)]) == 0  # the gate runs (stamps the tree)...
    _git(tmp_path, "commit", "-q", "-m", "verified", "--no-verify")  # ...then the commit lands

    (tmp_path / "b.py").write_text("def g(y):\n    return y\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "sneaky", "--no-verify")  # no gate run at all

    cov = gate_coverage(tmp_path)
    assert cov["commits"] == 2
    assert cov["verified"] == 1
    assert len(cov["unverified"]) == 1


def test_blocked_run_does_not_stamp(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    assert precommit_main([str(tmp_path)]) == 1  # duplicate -> blocked
    assert not (tmp_path / ".cartogate" / "gate_runs.jsonl").exists()


def test_stats_reports_the_coverage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "sneaky", "--no-verify")
    assert stats_run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "gate coverage" in out
    assert "unverified" in out.lower()


def test_merge_commits_are_not_accused(tmp_path: Path) -> None:
    """`git merge` never runs pre-commit — a merge commit must not count as bypassed."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base", "--no-verify")
    _git(tmp_path, "checkout", "-q", "-b", "side")
    (tmp_path / "b.py").write_text("def g(y):\n    return y\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "side", "--no-verify")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "c.py").write_text("def h(z):\n    return z\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "main2", "--no-verify")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "merge", "side")

    cov = gate_coverage(tmp_path)
    assert cov["commits"] == 3  # the merge commit is excluded entirely, not accused


def test_doctor_warns_on_unverified_commits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cartogate.doctor import _check_hooks, _Report

    _git(tmp_path, "init", "-q")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexec python -m cartogate.precommit\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "sneaky", "--no-verify")

    _check_hooks(tmp_path, _Report())  # _Report prints; capture via capsys
    out = capsys.readouterr().out
    assert "without a passing gate run" in out


def test_non_git_directory_reports_no_coverage(tmp_path: Path) -> None:
    assert gate_coverage(tmp_path) == {"commits": 0, "verified": 0, "unverified": []}
