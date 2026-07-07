"""Section 4 — shared surface logic for the enforcement hooks."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import MakeSymbol

from cartogate.schema.enums import Language
from cartogate.store import InMemoryStore
from cartogate.surfaces import (
    extract_proposed_text,
    find_duplicate_signatures,
    find_repo_root,
    gate_proposed_source,
    resolve_repo,
)


def test_find_duplicate_signatures(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.m1.add", signature="def add(a, b):")
    b = make_symbol("pkg.m2.add", signature="def add(a, b):")  # same sig, different qname
    c = make_symbol("pkg.m3.sub", signature="def sub(a, b):")  # different sig
    dups = find_duplicate_signatures([a, b, c])
    assert set(dups) == {(Language.PYTHON, "add(a,b)")}
    members = dups[(Language.PYTHON, "add(a,b)")]
    assert {n.qualified_name for n in members} == {"pkg.m1.add", "pkg.m2.add"}


def test_find_duplicate_ignores_same_qualified_name(make_symbol: MakeSymbol) -> None:
    a = make_symbol("pkg.m.add", signature="def add(a, b):")
    assert find_duplicate_signatures([a, a]) == {}


def test_find_duplicate_ignores_methods(make_symbol: MakeSymbol) -> None:
    # Two methods of different classes sharing a signature are normal OOP, not duplicate
    # code (e.g. an ABC and its impl, or unrelated __init__s). They must NOT be flagged.
    a = make_symbol("pkg.A.run", signature="def run(self, x):", is_top_level=False)
    b = make_symbol("pkg.B.run", signature="def run(self, x):", is_top_level=False)
    assert find_duplicate_signatures([a, b]) == {}


def test_gate_proposed_source_blocks_existing(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("m.py", [make_symbol("pkg.foo", signature="def foo(x):")], [])
    blocked = gate_proposed_source(store, "def foo(x):\n    return x\n")
    assert len(blocked) == 1
    assert blocked[0]["existing_qualified_name"] == "pkg.foo"


def test_gate_proposed_source_allows_novel(make_symbol: MakeSymbol) -> None:
    store = InMemoryStore()
    store.upsert_unit("m.py", [make_symbol("pkg.foo", signature="def foo(x):")], [])
    assert gate_proposed_source(store, "def bar(y, z):\n    return y\n") == []


def test_gate_excludes_the_symbol_being_edited(make_symbol: MakeSymbol) -> None:
    # Editing an existing symbol must NOT self-block: re-declaring `login` in the file that already
    # defines it is an edit, not a new duplicate (F-28).
    store = InMemoryStore()
    store.upsert_unit(
        "auth.py", [make_symbol("pkg.auth.login", signature="def login(user):", unit="auth.py")], []
    )
    src = "def login(user):\n    return user\n"

    # ...editing auth.py (where login lives) -> allowed (it's the symbol being edited).
    assert gate_proposed_source(store, src, editing_unit="auth.py") == []
    # ...writing the same login into a DIFFERENT file -> a real cross-file duplicate -> blocked.
    assert gate_proposed_source(store, src, editing_unit="other.py") != []
    # ...with no editing context (the explicit MCP "before I create" call) -> unchanged, blocks.
    assert gate_proposed_source(store, src) != []


def test_extract_proposed_text_joins_known_keys() -> None:
    assert "abc" in extract_proposed_text({"content": "abc"})
    assert extract_proposed_text({"new_string": "def f(): ..."}) == "def f(): ..."
    assert extract_proposed_text({"unrelated": 5}) == ""


# --- repo resolution (cross-repo auto-detection) --------------------------------------------


def test_find_repo_root_walks_up_to_git_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    # From a file deep inside the repo, the root is the dir holding `.git`.
    assert find_repo_root(nested / "m.py") == tmp_path
    assert find_repo_root(nested) == tmp_path


def test_find_repo_root_handles_git_worktree_file(tmp_path: Path) -> None:
    # A worktree/submodule records `.git` as a *file*, not a directory — still a root.
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    assert find_repo_root(tmp_path / "a.py") == tmp_path


def test_find_repo_root_none_when_no_marker(tmp_path: Path) -> None:
    assert find_repo_root(tmp_path / "loose.py") is None


def test_resolve_repo_prefers_explicit_override(tmp_path: Path) -> None:
    pin = tmp_path / "pinned"
    pin.mkdir()
    repo, repo_id = resolve_repo(
        "/anywhere/else/file.py", env={"CARTOGATE_REPO": str(pin)}, cwd=tmp_path
    )
    assert repo == pin.resolve()
    assert repo_id == "pinned"


def test_resolve_repo_autodetects_from_edited_file(tmp_path: Path) -> None:
    repo_dir = tmp_path / "myrepo"
    (repo_dir / "pkg").mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    edited = repo_dir / "pkg" / "m.py"
    # No CARTOGATE_REPO, cwd is unrelated — the repo is discovered from the file being edited.
    repo, repo_id = resolve_repo(str(edited), env={}, cwd=tmp_path)
    assert repo == repo_dir
    assert repo_id == "myrepo"


def test_resolve_repo_autodetects_from_cwd_when_no_file(tmp_path: Path) -> None:
    repo_dir = tmp_path / "svc"
    sub = repo_dir / "a" / "b"
    sub.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    # MCP-server case: no edited file, so the project root of cwd is used.
    repo, _ = resolve_repo(None, env={}, cwd=sub)
    assert repo == repo_dir


def test_resolve_repo_falls_back_to_cwd(tmp_path: Path) -> None:
    repo, repo_id = resolve_repo(None, env={}, cwd=tmp_path)
    assert repo == tmp_path.resolve()


def test_resolve_repo_id_override(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _, repo_id = resolve_repo(None, env={"CARTOGATE_REPO_ID": "custom"}, cwd=tmp_path)
    assert repo_id == "custom"
