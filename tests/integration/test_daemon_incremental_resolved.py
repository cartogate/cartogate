"""F-36 Stage B: the daemon refreshes a RESOLVED graph incrementally (re-extract only the changed
file, resolved against the whole repo via a ResolutionContext) — with sound fallbacks to a full
rebuild for the cases incremental can't do safely (rename/removal, deletion, shared module).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.schema.enums import EdgeType
from cartogate.store import InMemoryStore


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


def _calls(store: InMemoryStore) -> set[tuple[str, str]]:
    sub = store.subgraph(store.visible_node_ids(), edge_types=(EdgeType.CALLS,))
    by_id = {n.id: n for n in sub.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in sub.edges
        if e.src in by_id and e.dst in by_id
    }


def _refresher(repo: Path) -> GitLazyRefresh:
    return GitLazyRefresh(repo, repo_id="repo", resolve=True, index_docs=False, clock=_Clock(),
                          debounce_s=0.0)


def test_body_edit_is_incremental_and_keeps_cross_file_resolution(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    refresh = _refresher(repo)
    store = refresh.prime()
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in _calls(store)

    # Add a second function in b.py that also calls helper (qnames are a SUPERSET — no removal).
    (repo / "pkg" / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef run():\n    return helper()\n\n\n"
        "def extra():\n    return helper() + 1\n",
        encoding="utf-8",
    )
    new = refresh.maybe_refresh()
    assert new is not None
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "incremental"  # only b.py re-extracted
    assert refresh.last_refresh.reindexed == 1
    calls = _calls(new)
    assert ("repo.pkg.b.run", "repo.pkg.a.helper") in calls  # the old cross-file edge survived
    assert ("repo.pkg.b.extra", "repo.pkg.a.helper") in calls  # NEW edge resolved vs unchanged a.py


def test_rename_falls_back_to_full(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    refresh = _refresher(repo)
    refresh.prime()
    # Rename helper -> helper2 in a.py: a removed an exported symbol, so b's incoming edge would
    # dangle on an incremental refresh — must fall back to a full rebuild.
    (repo / "pkg" / "a.py").write_text("def helper2():\n    return 1\n", encoding="utf-8")
    new = refresh.maybe_refresh()
    assert new is not None
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"


def test_deletion_falls_back_to_full(tmp_path: Path) -> None:
    repo = _py_repo(tmp_path)
    refresh = _refresher(repo)
    refresh.prime()
    (repo / "pkg" / "b.py").unlink()  # a deletion dangles incoming resolved edges -> full rebuild
    new = refresh.maybe_refresh()
    assert new is not None
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"


def _commit_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def test_same_qname_identity_change_falls_back_to_full(tmp_path: Path) -> None:
    # The id-based (not qname-based) check: a file with two same-qname defs (distinct stmt_ordinal
    # ids) edited down to one def keeps the qname but DROPS a node id — an unchanged file's edge to
    # the old id would dangle, so this must full-rebuild. A qname-set check would miss this.
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        "def f(x):\n    return x\n\n\ndef f(x, y):\n    return x + y\n", encoding="utf-8"
    )
    (pkg / "b.py").write_text("from pkg.a import f\n\n\ndef run():\n    return f(1)\n", "utf-8")
    _commit_repo(repo)
    refresh = _refresher(repo)
    refresh.prime()

    (pkg / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")  # 2 defs -> 1
    refresh.maybe_refresh()
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"


def test_method_removal_falls_back_to_full(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        "class A:\n    def m(self):\n        return 1\n\n    def keep(self):\n        return 2\n",
        encoding="utf-8",
    )
    _commit_repo(repo)
    refresh = _refresher(repo)
    refresh.prime()

    (pkg / "a.py").write_text("class A:\n    def keep(self):\n        return 2\n", encoding="utf-8")
    refresh.maybe_refresh()
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"  # removed method A.m -> incoming edge could dangle


def test_large_batch_falls_back_to_full(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for i in range(21):  # 21 unique-module files > _MAX_INCREMENTAL_FILES (20)
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    _commit_repo(repo)
    refresh = _refresher(repo)
    refresh.prime()

    for i in range(21):  # touch all of them
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    return {i} + 1\n", encoding="utf-8")
    refresh.maybe_refresh()
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"  # >20 changed -> incremental isn't cheaper


def test_shared_module_edit_falls_back_to_full(tmp_path: Path) -> None:
    # A Go package's files share one module node; re-extracting one in isolation would mis-own it,
    # so a touch must full-rebuild (regardless of resolve).
    repo = tmp_path / "gomod"
    pkg = repo / "calc"
    pkg.mkdir(parents=True)
    (pkg / "a.go").write_text("package calc\n\nfunc Alpha() int { return 1 }\n", encoding="utf-8")
    (pkg / "b.go").write_text("package calc\n\nfunc Beta() int { return 2 }\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    refresh = _refresher(repo)
    refresh.prime()
    (pkg / "a.go").write_text(
        "package calc\n\nfunc Alpha() int { return 1 }\n\nfunc Gamma() int { return 3 }\n", "utf-8"
    )
    new = refresh.maybe_refresh()
    assert new is not None
    assert refresh.last_refresh is not None
    assert refresh.last_refresh.mode == "full"
