import json

from cartogate.audit import ledger


def _seed(tmp_path):
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={"n": 0}, env={})
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={"n": 1}, env={})
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={"n": 2}, env={})


def _rewrite(tmp_path, lines):
    ledger.ledger_path(tmp_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_intact_chain_verifies(tmp_path):
    _seed(tmp_path)
    assert ledger.verify(tmp_path).ok is True


def test_edited_field_without_rehash_is_caught(tmp_path):
    _seed(tmp_path)
    lines = ledger._read_raw(tmp_path)
    e = json.loads(lines[1])
    e["evidence"] = {"n": 999}
    lines[1] = json.dumps(e)
    _rewrite(tmp_path, lines)
    res = ledger.verify(tmp_path)
    assert res.ok is False and res.failure_seq == 1


def test_deleted_middle_line_is_caught(tmp_path):
    _seed(tmp_path)
    lines = ledger._read_raw(tmp_path)
    _rewrite(tmp_path, [lines[0], lines[2]])
    assert ledger.verify(tmp_path).ok is False


def test_reordered_lines_are_caught(tmp_path):
    _seed(tmp_path)
    lines = ledger._read_raw(tmp_path)
    _rewrite(tmp_path, [lines[1], lines[0], lines[2]])
    assert ledger.verify(tmp_path).ok is False


def test_corrupt_json_line_is_caught(tmp_path):
    _seed(tmp_path)
    lines = ledger._read_raw(tmp_path)
    lines[1] = "{not json"
    _rewrite(tmp_path, lines)
    assert ledger.verify(tmp_path).ok is False


def test_empty_ledger_is_valid(tmp_path):
    assert ledger.verify(tmp_path).ok is True


def test_tail_edit_with_rehash_is_a_known_bound(tmp_path):
    # DOCUMENTED LIMITATION (docs/AUDIT.md security bound): a bare hash chain cannot detect an
    # edit-and-rehash of the LAST entry — nothing downstream references its hash. This test pins
    # the honest bound so no one later assumes the tail is protected.
    _seed(tmp_path)
    lines = ledger._read_raw(tmp_path)
    e = json.loads(lines[-1])
    e["evidence"] = {"n": 999}
    e.pop("hash")
    e["hash"] = ledger._entry_hash(e)  # attacker recomputes the tampered tail entry's hash
    lines[-1] = json.dumps(e)
    _rewrite(tmp_path, lines)
    assert ledger.verify(tmp_path).ok is True  # NOT caught — the accepted tail bound


def test_tail_truncation_is_a_known_bound(tmp_path):
    # DOCUMENTED LIMITATION: dropping the last entry leaves a valid shorter chain. (A removed
    # commit_pass still resurfaces via the git coverage report — see test_ledger_verify_anchor.)
    _seed(tmp_path)
    _rewrite(tmp_path, ledger._read_raw(tmp_path)[:-1])
    assert ledger.verify(tmp_path).ok is True  # NOT caught by the chain alone
