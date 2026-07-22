"""The file walk must PRUNE excluded trees and survive filesystem faults (the pnpm crash).

A real user daemon died at prime: ``rglob`` physically descended into a pnpm ``node_modules``
store whose nesting exceeds Windows' path limit / holds dangling junctions, raising
``FileNotFoundError`` out of the walk and killing the whole index. The walk must never enter
excluded trees at all, and any unreadable directory must be skipped, not fatal.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cartogate.extract.walk import iter_files


def test_walk_prunes_excluded_trees_instead_of_filtering(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("# hi", encoding="utf-8")
    deep = tmp_path / "node_modules" / ".pnpm" / "pkg@1.0.0" / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    (deep / "CHANGELOG.md").write_text("# nope", encoding="utf-8")

    found = list(iter_files(tmp_path, ".md"))
    assert [p.name for p in found] == ["readme.md"]  # vendored markdown never surfaces


def test_walk_survives_a_vanishing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a directory that errors mid-walk (dangling junction / path too long /
    vanished) must be SKIPPED — the rest of the tree still indexes and nothing raises."""
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "a.md").write_text("# a", encoding="utf-8")
    bad = tmp_path / "haunted"
    bad.mkdir()

    real_scandir = os.scandir

    def _flaky(path: object = ".") -> object:
        if os.path.basename(str(path)) == "haunted":
            raise FileNotFoundError(3, "The system cannot find the path specified", str(path))
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", _flaky)
    found = list(iter_files(tmp_path, ".md"))  # must not raise
    assert [p.name for p in found] == ["a.md"]


def test_walk_skips_unstatable_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.md").write_text("# a", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b", encoding="utf-8")

    real_scandir = os.scandir

    class _FlakyEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            if self.name == "b.md":
                raise OSError("stat failed")
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def is_symlink(self) -> bool:
            return self._entry.is_symlink()

    class _Wrapper:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __enter__(self) -> object:
            return (_FlakyEntry(e) for e in self._inner.__enter__())  # type: ignore[attr-defined]

        def __exit__(self, *exc: object) -> None:
            self._inner.__exit__(*exc)  # type: ignore[attr-defined]

    monkeypatch.setattr(os, "scandir", lambda p=".": _Wrapper(real_scandir(p)))
    found = list(iter_files(tmp_path, ".md"))  # must not raise; the good entry still indexes
    assert [p.name for p in found] == ["a.md"]
