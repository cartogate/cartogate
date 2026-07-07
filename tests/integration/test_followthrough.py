"""Phase 2 finale — follow-through + deletion advisories (claims-vs-facts, scope-report halves).

Claims-vs-facts, operationalized deterministically: no parsing what the agent SAYS — when a
contract CHANGED, report the follow-through facts from the resolved snapshot (callers not in
this commit, covering tests untouched, referencing docs untouched). And the anchor-free half of
the scope report: symbols DELETED while live references remain. Both read the daemon-maintained
snapshot in-process — and because a snapshot can LAG the repo, every cited unit passes a
persist-time content-hash freshness guard (stale evidence is never cited), and a snapshot from
a differently-named checkout is rejected. Without a usable snapshot: silent. Advisory-only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.precommit import main as precommit_main
from cartogate.store import InMemoryStore
from cartogate.store.persist import graph_path, save_graph

AUTH = "def login(user):\n    return user\n"
APP = "from auth import login\n\ndef run():\n    return login('a')\n"
TEST = "from auth import login\n\ndef test_login():\n    assert login('x') == 'x'\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"],
        },
    )


def _seed_with_snapshot(repo: Path) -> None:
    _git(repo, "init", "-q")
    (repo / "auth.py").write_text(AUTH, encoding="utf-8")
    (repo / "app.py").write_text(APP, encoding="utf-8")
    (repo / "test_auth.py").write_text(TEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", "--no-verify")
    # The daemon would maintain this; tests persist it directly (resolved graph).
    store = InMemoryStore()
    index_package(repo, repo_id=repo.name, store=store, resolve=True)
    save_graph(store, graph_path(repo), repo_id=repo.name, base=repo.parent)


def test_contract_change_reports_untouched_callers_and_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_snapshot(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user, tenant):\n    return user\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "ADVISORY:" in err
    assert "follow-through" in err
    assert "caller(s) not in this commit" in err and "app.py" in err
    assert "covering test(s) untouched" in err


def test_updating_the_callers_quiets_the_followthrough(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_snapshot(tmp_path)
    (tmp_path / "auth.py").write_text(
        "def login(user, tenant):\n    return user\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "from auth import login\n\ndef run():\n    return login('a', 't')\n", encoding="utf-8"
    )
    (tmp_path / "test_auth.py").write_text(
        "from auth import login\n\ndef test_login():\n    assert login('x', 't') == 'x'\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "ADVISORY:" in err  # the signature change is still reported...
    assert "caller(s) not in this commit" not in err  # ...but the follow-through is clean


def test_deleting_a_referenced_symbol_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_snapshot(tmp_path)
    (tmp_path / "auth.py").write_text("x = 1\n", encoding="utf-8")  # login deleted
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER affects the exit
    err = capsys.readouterr().err
    assert "DELETION ADVISORY" in err
    assert "login" in err and "live reference" in err
    assert "ACTION:" in err


def test_deleting_an_unreferenced_symbol_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_with_snapshot(tmp_path)
    (tmp_path / "solo.py").write_text("def lonely():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "solo.py")
    _git(tmp_path, "commit", "-q", "-m", "solo", "--no-verify")
    # refresh the snapshot so lonely() exists in it, unreferenced
    store = InMemoryStore()
    index_package(tmp_path, repo_id=tmp_path.name, store=store, resolve=True)
    save_graph(store, graph_path(tmp_path), repo_id=tmp_path.name, base=tmp_path.parent)

    (tmp_path / "solo.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "solo.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "DELETION ADVISORY" not in capsys.readouterr().err


def test_stale_snapshot_evidence_is_never_cited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review HIGH-1 (reproduced by the reviewer): the snapshot can lag the repo — a
    caller that was REMOVED in an earlier, already-committed change must not be accused
    (its unit fails the persist-time content-hash freshness guard)."""
    _seed_with_snapshot(tmp_path)  # snapshot: app.py calls login
    # An already-committed later change removes the call; the snapshot is NOT refreshed.
    (tmp_path / "app.py").write_text(
        "def run():" + chr(10) + "    return 1" + chr(10), encoding="utf-8"
    )
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-q", "-m", "remove call", "--no-verify")
    # Now the commit under test: a signature change to login.
    (tmp_path / "auth.py").write_text(
        "def login(user, tenant):" + chr(10) + "    return user" + chr(10), encoding="utf-8"
    )
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "ADVISORY:" in err  # the signature change itself is still reported
    assert "app.py" not in err  # ...but the stale caller is never cited


def test_mismatched_snapshot_repo_id_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review HIGH-2 (reproduced by the reviewer): a snapshot persisted under a different
    directory name has incomparable unit strings — reject it (silent), never fail open."""
    import gzip
    import json

    _seed_with_snapshot(tmp_path)
    from cartogate.store.persist import graph_path

    snap = graph_path(tmp_path)
    with gzip.open(snap, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["repo_id"] = "someothername"  # simulate a differently-named checkout
    with gzip.open(snap, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    import cartogate.precommit as pc

    pc._snapshot_tools.cache_clear()  # the lru would otherwise serve the pre-tamper load
    (tmp_path / "auth.py").write_text("x = 1" + chr(10), encoding="utf-8")  # deletes login
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "DELETION ADVISORY" not in capsys.readouterr().err  # rejected, not fail-open


def test_daemon_refresh_keeps_the_snapshot_fresh(tmp_path: Path) -> None:
    """Root cause of HIGH-1: maybe_refresh() now persists the snapshot after every applied
    refresh, so commit-time readers see the daemon-current graph, not daemon-start."""
    from cartogate.daemon.refresh import GitLazyRefresh
    from cartogate.store.persist import graph_path

    (tmp_path / "a.py").write_text(
        "def f(x):" + chr(10) + "    return x" + chr(10), encoding="utf-8"
    )
    refresh = GitLazyRefresh(tmp_path, repo_id=tmp_path.name, resolve=True, debounce_s=0.0)
    refresh.prime()
    before = graph_path(tmp_path).read_bytes()
    (tmp_path / "b.py").write_text(
        "def g(y):" + chr(10) + "    return y" + chr(10), encoding="utf-8"
    )
    assert refresh.maybe_refresh() is not None  # a refresh was applied...
    assert graph_path(tmp_path).read_bytes() != before  # ...and persisted


def test_without_a_snapshot_both_stay_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No resolved graph -> no reference evidence -> say nothing rather than guess."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "auth.py").write_text(AUTH, encoding="utf-8")
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    (tmp_path / "auth.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "auth.py")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "DELETION ADVISORY" not in err
    assert "follow-through" not in err
