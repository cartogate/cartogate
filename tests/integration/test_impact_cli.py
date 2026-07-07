"""``cartogate impact`` CLI — end-to-end over a real git repo (index + diff + report)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cartogate import impact_cli


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        "def target():\n    return 1\n\n\ndef caller():\n    return target()\n", encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_impact_cli_reports_changed_and_affected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with_commit(tmp_path)
    # Modify target's body (working-tree change vs HEAD).
    (repo / "pkg" / "core.py").write_text(
        "def target():\n    return 2\n\n\ndef caller():\n    return target()\n", encoding="utf-8"
    )
    rc = impact_cli.main([str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Cartogate impact summary" in out
    assert "pkg.core.target" in out  # the changed symbol
    assert "pkg.core.caller" in out  # its caller -> affected (blast radius)


def test_impact_cli_json_and_clean_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with_commit(tmp_path)  # no working-tree changes
    rc = impact_cli.main([str(repo), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["counts"]["changed"] == 0  # nothing changed -> empty summary


def test_impact_cli_ref_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo_with_commit(tmp_path)
    # Commit a change, then diff HEAD~1..HEAD via --ref.
    (repo / "pkg" / "core.py").write_text(
        "def target():\n    return 2\n\n\ndef caller():\n    return target()\n", encoding="utf-8"
    )
    _git(repo, "commit", "-am", "change target")
    rc = impact_cli.main([str(repo), "--ref", "HEAD~1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pkg.core.target" in out  # the committed change is reported


def test_impact_cli_subdir_root_is_graceful(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Pointing at a (non-package) SUBDIR misaligns git-relative vs index-relative paths -> a
    # graceful empty summary (a documented under-report), never a crash.
    repo = _repo_with_commit(tmp_path)
    sub = repo / "extra"  # not a package (no __init__) -> indexes cleanly under its own root
    sub.mkdir()
    (sub / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add helper")
    (sub / "helper.py").write_text("def helper():\n    return 2\n", encoding="utf-8")  # change it
    rc = impact_cli.main([str(sub), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["counts"]["changed"] == 0  # misaligned paths -> empty, not a crash
