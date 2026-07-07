"""Tests for GitLazyRefresh — the git-lazy freshness floor.

Detects working-tree change (git porcelain + file mtimes), debounces rapid checks, and rebuilds
a fresh structural store on change. A controllable clock makes the debounce deterministic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from cartogate.daemon import refresh as refresh_mod
from cartogate.daemon.refresh import GitLazyRefresh


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _spy_paths(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record the ``paths`` argument of every index_package call the refresher makes.

    ``None`` => a full rebuild; a list => an incremental re-parse of exactly those files.
    """
    calls: list[object] = []
    real = refresh_mod.index_package

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("paths"))
        return real(*args, **kwargs)

    monkeypatch.setattr(refresh_mod, "index_package", spy)
    return calls


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_prime_indexes_and_debounce_suppresses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    clock = FakeClock()
    refresh = GitLazyRefresh(repo, repo_id="t", clock=clock, debounce_s=0.25)

    store = refresh.prime()
    assert store.exists("alpha()") is True

    # Within the debounce window -> no refresh.
    assert refresh.maybe_refresh() is None
    # Past the debounce window but nothing changed -> still no refresh.
    clock.t += 1.0
    assert refresh.maybe_refresh() is None


def test_refresh_picks_up_a_new_function(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    clock = FakeClock()
    refresh = GitLazyRefresh(repo, repo_id="t", clock=clock, debounce_s=0.25)
    refresh.prime()

    # Edit a tracked file -> git porcelain marks it modified -> refresh fires.
    (repo / "pkg" / "m.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    clock.t += 1.0
    new_store = refresh.maybe_refresh()
    assert new_store is not None
    assert new_store.exists("beta()") is True
    assert new_store.exists("alpha()") is True


def test_refresh_reflects_a_deleted_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "pkg" / "other.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add other")
    clock = FakeClock()
    refresh = GitLazyRefresh(repo, repo_id="t", clock=clock, debounce_s=0.0)
    store = refresh.prime()
    assert store.exists("gamma()") is True

    (repo / "pkg" / "other.py").unlink()
    clock.t += 1.0
    new_store = refresh.maybe_refresh()
    assert new_store is not None
    assert new_store.exists("gamma()") is False


def test_incremental_reparses_only_the_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)  # pkg/__init__.py + pkg/m.py (alpha)
    (repo / "pkg" / "n.py").write_text("def delta():\n    return 4\n", encoding="utf-8")
    refresh = GitLazyRefresh(repo, repo_id="t", clock=FakeClock(), debounce_s=0.0)
    refresh.prime()

    calls = _spy_paths(monkeypatch)  # spy after prime, so we only see the refresh's call
    (repo / "pkg" / "m.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    store = refresh.maybe_refresh()

    assert store is not None
    assert store.exists("beta()")  # the changed file's new symbol
    assert store.exists("delta()")  # an unchanged file's symbol carried forward
    # Incremental: index_package was called for *only* the changed file (not a full rebuild).
    assert len(calls) == 1
    assert calls[0] is not None
    assert [Path(p).name for p in calls[0]] == ["m.py"]


def test_incremental_picks_up_an_added_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    refresh = GitLazyRefresh(repo, repo_id="t", clock=FakeClock(), debounce_s=0.0)
    refresh.prime()

    calls = _spy_paths(monkeypatch)
    (repo / "pkg" / "added.py").write_text("def epsilon():\n    return 5\n", encoding="utf-8")
    store = refresh.maybe_refresh()

    assert store is not None
    assert store.exists("epsilon()")  # the new file
    assert store.exists("alpha()")  # the untouched file
    assert calls and calls[0] is not None
    assert [Path(p).name for p in calls[0]] == ["added.py"]


def test_shared_module_change_falls_back_to_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Go package's files share one module node, so re-parsing one file in isolation would mis-own
    # it — such a change must trigger a full rebuild, not the incremental path.
    repo = tmp_path / "gomod"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.go").write_text("package pkg\n\nfunc Alpha() int { return 1 }\n", "utf-8")
    (repo / "pkg" / "b.go").write_text("package pkg\n\nfunc Beta() int { return 2 }\n", "utf-8")
    refresh = GitLazyRefresh(repo, repo_id="t", clock=FakeClock(), debounce_s=0.0)
    refresh.prime()

    calls = _spy_paths(monkeypatch)
    (repo / "pkg" / "a.go").write_text(
        "package pkg\n\nfunc Alpha() int { return 1 }\n\nfunc Gamma() int { return 3 }\n", "utf-8"
    )
    store = refresh.maybe_refresh()

    assert store is not None
    names = {store.get_node(i).name for i in store.visible_node_ids()}  # type: ignore[union-attr]
    assert {"Alpha", "Beta", "Gamma"} <= names  # correct: the change is reflected
    # ...via a FULL rebuild: it re-parses BOTH package files (not just the changed a.go, which is
    # what the incremental path would pass). The full set is passed as `paths` to skip a 2nd scan.
    assert calls and calls[0] is not None
    assert {Path(p).name for p in calls[0]} == {"a.go", "b.go"}


def test_non_git_directory_uses_mtime_fallback(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    clock = FakeClock()
    refresh = GitLazyRefresh(root, repo_id="t", clock=clock, debounce_s=0.0)
    store = refresh.prime()
    assert store.exists("alpha()") is True
    # No change -> no refresh.
    clock.t += 1.0
    assert refresh.maybe_refresh() is None
