"""F-09 Stage 3: the resolved daemon loads a persisted snapshot on cold start instead of rebuilding,
then re-extracts only files changed since (reusing the F-36 incremental machinery), and persists the
graph after a build. Closes the cold-start cost (reboot / fresh clone / CI / killed daemon).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.schema.enums import EdgeType
from cartogate.store import InMemoryStore
from cartogate.store.persist import graph_path


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _py_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _refresher(repo: Path, *, resolve: bool = True) -> GitLazyRefresh:
    return GitLazyRefresh(
        repo, repo_id="repo", resolve=resolve, index_docs=False, clock=_Clock(), debounce_s=0.0
    )


def _calls(store: InMemoryStore) -> set[tuple[str, str]]:
    sub = store.subgraph(store.visible_node_ids(), edge_types=(EdgeType.CALLS,))
    by_id = {n.id: n for n in sub.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in sub.edges
        if e.src in by_id and e.dst in by_id
    }


def test_first_prime_builds_and_persists(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    refresh = _refresher(repo)
    store = refresh.prime()
    assert refresh.last_refresh is not None and refresh.last_refresh.mode == "full"
    assert graph_path(repo).exists()  # the snapshot was written for next time
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in _calls(store)


def test_cold_start_loads_unchanged_snapshot(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist
    # A brand-new refresher (a "restart") loads the snapshot instead of rebuilding — instant.
    cold = _refresher(repo)
    store = cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "snapshot"
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in _calls(store)  # graph is correct


def test_cold_start_applies_delta_for_changed_file(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist
    # Edit b.py (add a function) after the snapshot, then cold-start.
    (repo / "pkg" / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef run():\n    return helper()\n\n\n"
        "def extra():\n    return helper() + 1\n",
        encoding="utf-8",
    )
    cold = _refresher(repo)
    store = cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "snapshot+delta"
    assert cold.last_refresh.reindexed == 1  # only b.py re-extracted
    calls = _calls(store)
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in calls  # carried-forward edge intact
    assert ("repo.pkg.b.extra", "repo.pkg.a.helper") in calls  # new edge resolved vs unchanged a.py


def test_cold_start_added_file_applies_delta(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist (snapshot has a.py, b.py)
    # A brand-new file (absent from the snapshot) -> reextract just it, resolved vs the snapshot.
    (repo / "pkg" / "c.py").write_text(
        "from pkg.a import helper\n\n\ndef extra():\n    return helper()\n", encoding="utf-8"
    )
    cold = _refresher(repo)
    store = cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "snapshot+delta"
    assert ("repo.pkg.c.extra", "repo.pkg.a.helper") in _calls(store)  # the new file resolved
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in _calls(store)  # the snapshot's edges intact


def test_cold_start_deleted_file_falls_back_to_full(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist
    (repo / "pkg" / "b.py").unlink()  # a deletion dangles incoming resolved edges -> full rebuild
    cold = _refresher(repo)
    store = cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "full"
    assert store.get_symbol("repo.pkg.b.run") is None  # the deleted file's symbols are gone


def test_corrupt_snapshot_falls_back_to_full(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist
    graph_path(repo).write_bytes(b"not gzip")  # a torn/corrupt snapshot must degrade to a rebuild
    cold = _refresher(repo)
    cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "full"


def test_cold_start_rename_falls_back_to_full(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # build + persist
    # Rename helper in a.py: b's incoming edge would dangle on an incremental delta -> full rebuild.
    (repo / "pkg" / "a.py").write_text("def helper2():\n    return 1\n", encoding="utf-8")
    cold = _refresher(repo)
    cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "full"


def test_cold_start_rejects_a_foreign_repo_id_snapshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A snapshot whose repo_id doesn't match is rejected + logged, and the graph rebuilds — this is
    the likely cause when a snapshot was written while resolving the wrong repo."""
    repo = _py_repo(tmp_path)
    _refresher(repo).prime()  # writes a snapshot stamped repo_id="repo"
    foreign = GitLazyRefresh(
        repo, repo_id="other", resolve=True, index_docs=False, clock=_Clock(), debounce_s=0.0
    )
    with caplog.at_level(logging.INFO, logger="cartogate"):
        foreign.prime()
    assert foreign.last_refresh is not None and foreign.last_refresh.mode == "full"
    assert any("repo_id" in r.message for r in caplog.records)


def test_structural_daemon_does_not_persist(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    _refresher(repo, resolve=False).prime()
    assert not graph_path(repo).exists()  # persistence is for the heavy resolved graph only


def test_index_cli_persists_a_loadable_snapshot(tmp_path: Path) -> None:
    from cartogate.index_cli import cmd_index
    from cartogate.store.persist import load_graph

    repo = _py_repo(tmp_path)
    assert cmd_index(repo) == 0
    assert graph_path(repo).exists()
    loaded = load_graph(graph_path(repo))
    assert loaded is not None
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in _calls(loaded.store)

    # ...and a resolved daemon cold-starts from it (no rebuild) since nothing changed.
    cold = _refresher(repo)
    cold.prime()
    assert cold.last_refresh is not None and cold.last_refresh.mode == "snapshot"


def test_index_cli_second_run_is_incremental(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The headline of the auto-refresh work: a repeat `cartogate index` loads + deltas, not a full
    rebuild — so running it on every commit is cheap."""
    from cartogate.index_cli import cmd_index

    repo = _py_repo(tmp_path)
    assert cmd_index(repo) == 0  # first run: full build + persist
    capsys.readouterr()
    assert cmd_index(repo) == 0  # second run: repo unchanged -> snapshot load
    assert "snapshot" in capsys.readouterr().out  # not "full"
