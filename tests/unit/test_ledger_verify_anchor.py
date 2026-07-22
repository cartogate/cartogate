import subprocess

from cartogate.audit import ledger


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _commit_tree(repo, name, body):
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")
    out = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _init(repo):
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "T")


def test_matching_commit_entry_verifies(tmp_path):
    _init(tmp_path)
    tree = _commit_tree(tmp_path, "a.py", "x = 1\n")
    ledger.append(tmp_path, entry_type="commit_pass", tree=tree, evidence={}, env={})
    res = ledger.verify(tmp_path)
    assert res.ok is True
    assert res.coverage["verified"] == 1


def test_uncommitted_pass_tree_is_pending_not_a_failure(tmp_path):
    # A commit_pass is stamped at pre-commit time, BEFORE the commit object exists. If the commit
    # then aborts, its tree never enters history — that is PENDING/aborted, never "TAMPERED".
    _init(tmp_path)
    _commit_tree(tmp_path, "a.py", "x = 1\n")
    ledger.append(tmp_path, entry_type="commit_pass", tree="deadbeef" * 5, evidence={}, env={})
    res = ledger.verify(tmp_path)
    assert res.ok is True                 # integrity is intact — the chain is valid
    assert res.coverage["pending"] == 1   # ...but the uncommitted stamp is surfaced, not failed


def test_bypassed_commit_is_reported_not_failed(tmp_path):
    _init(tmp_path)
    _commit_tree(tmp_path, "a.py", "x = 1\n")
    res = ledger.verify(tmp_path)
    assert res.ok is True
    assert len(res.coverage["unverified"]) == 1


def test_deleted_pass_tail_resurfaces_as_an_unverified_commit(tmp_path):
    # The hash chain cannot detect deletion of the TAIL entry (nothing downstream references it),
    # but a removed commit_pass tail still resurfaces here: its commit now has no stamp.
    _init(tmp_path)
    tree = _commit_tree(tmp_path, "a.py", "x = 1\n")
    ledger.append(tmp_path, entry_type="commit_pass", tree=tree, evidence={}, env={})
    assert ledger.verify(tmp_path).coverage["unverified"] == []
    lines = ledger._read_raw(tmp_path)
    body = "\n".join(lines[:-1])
    ledger.ledger_path(tmp_path).write_text(body + ("\n" if body else ""), encoding="utf-8")
    res = ledger.verify(tmp_path)
    assert res.ok is True                                # chain still "intact" — the tail bound
    assert len(res.coverage["unverified"]) == 1          # ...but the missing stamp shows here
