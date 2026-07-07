"""``cartogate localize`` CLI — end-to-end over a real git repo (index + diff + rank)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cartogate import localize_cli


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    (repo / "pkg" / "test_core.py").write_text(
        "from pkg.core import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_localize_cli_working_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    # Break helper in the working tree.
    (repo / "pkg" / "core.py").write_text(
        "def helper():\n    return 2\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    rc = localize_cli.main(["pkg.test_core.test_run", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pkg.core.helper" in out  # the changed symbol the test reaches


def test_localize_cli_ref_mode_committed_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The committed-cause case: a change committed on the branch, diffed via --ref (no work tree).
    repo = _repo(tmp_path)
    (repo / "pkg" / "core.py").write_text(
        "def helper():\n    return 2\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    _git(repo, "commit", "-am", "change helper")
    rc = localize_cli.main(["pkg.test_core.test_run", str(repo), "--ref", "HEAD~1", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["found"] is True
    assert any(s["qualified_name"] == "pkg.core.helper" for s in data["suspects"])
