"""Gitignore-aware indexing (F-38): inside a git repo, index only tracked + untracked-not-ignored
files; outside one, fall back to the fixed noise-dir walk.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cartogate.extract.pipeline import git_tracked_files, index_package, iter_source_files
from cartogate.store import InMemoryStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_gitignore_excludes_vendored_files(tmp_path: Path) -> None:
    _git(["init"], tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "kept.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    (pkg / "untracked.py").write_text("def fresh():\n    return 2\n", encoding="utf-8")
    (pkg / "vendored.py").write_text("def vendored():\n    return 3\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("pkg/vendored.py\n", encoding="utf-8")
    _git(["add", "pkg/__init__.py", "pkg/kept.py", ".gitignore"], tmp_path)

    working_set = git_tracked_files(tmp_path)
    assert working_set is not None
    names = {p.name for p in working_set}
    assert "kept.py" in names  # tracked
    assert "untracked.py" in names  # untracked but not ignored
    assert "vendored.py" not in names  # gitignored -> excluded

    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    qnames = {n.qualified_name for n in result.nodes}
    assert "pkg.kept.kept" in qnames
    assert "pkg.untracked.fresh" in qnames
    assert "pkg.vendored.vendored" not in qnames  # the ignored file was never indexed


def test_empty_repo_returns_empty_list_not_none(tmp_path: Path) -> None:
    # A freshly-init'd repo has an empty working set -> [] (NOT None, which means "not a repo").
    _git(["init"], tmp_path)
    assert git_tracked_files(tmp_path) == []


def test_empty_source_set_does_not_fall_back_to_rglob(tmp_path: Path) -> None:
    # The working set is non-None but has no source files; we must NOT fall back to rglob (which
    # would pick up the gitignored file). The []-vs-None distinction is the load-bearing invariant.
    _git(["init"], tmp_path)
    (tmp_path / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("def x():\n    return 1\n", encoding="utf-8")
    names = {p.name for p, _ in iter_source_files(tmp_path)}
    assert "ignored.py" not in names  # no rglob fallback despite the empty source set


def test_subdir_of_repo_scans_only_subdir(tmp_path: Path) -> None:
    _git(["init"], tmp_path)
    (tmp_path / "outside.py").write_text("def out():\n    return 1\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inside.py").write_text("def ins():\n    return 2\n", encoding="utf-8")
    names = {p.name for p in (git_tracked_files(sub) or [])}
    assert "inside.py" in names and "outside.py" not in names  # pathspec '.' scopes to root


def test_filename_with_space(tmp_path: Path) -> None:
    _git(["init"], tmp_path)
    (tmp_path / "a b.py").write_text("def s():\n    return 1\n", encoding="utf-8")
    names = {p.name for p in (git_tracked_files(tmp_path) or [])}
    assert "a b.py" in names  # the -z NUL-separated parse handles spaces


def test_non_git_dir_falls_back_to_fixed_walk(tmp_path: Path) -> None:
    # Not a git repo -> git_tracked_files is None and the rglob/excluded-dir walk is used.
    assert git_tracked_files(tmp_path) is None
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text("def m():\n    return 1\n", encoding="utf-8")
    (pkg / ".venv").mkdir()
    (pkg / ".venv" / "junk.py").write_text("def junk():\n    return 9\n", encoding="utf-8")

    names = {p.name for p, _ in iter_source_files(tmp_path)}
    assert "m.py" in names  # real source found
    assert "junk.py" not in names  # .venv still skipped by the fixed-dir fallback
