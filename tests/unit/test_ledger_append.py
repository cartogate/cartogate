from cartogate.audit import ledger


def test_append_builds_a_linked_chain(tmp_path):
    ledger.append(tmp_path, entry_type="commit_pass", tree="t0", evidence={"n": 1}, env={})
    ledger.append(tmp_path, entry_type="commit_block", tree="t1", evidence={"n": 2}, env={})
    entries = ledger.read(tmp_path)
    assert [e["seq"] for e in entries] == [0, 1]
    assert entries[0]["prev"] == ""
    assert entries[1]["prev"] == entries[0]["hash"]
    assert entries[0]["type"] == "commit_pass"
    assert entries[0]["decision_hash"] == ledger.decision_hash("commit_pass", "t0", {"n": 1})
    assert all(len(e["hash"]) == 128 for e in entries)


def test_append_is_fail_open_on_unwritable_target(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "ensure_state_dir", lambda repo: (_ for _ in ()).throw(OSError()))
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={}, env={})
    assert ledger.read(tmp_path) == []


def test_append_survives_a_malformed_forged_tail(tmp_path):
    """Re-verification 6b follow-on: a hand-appended line without seq/hash must not silently
    wedge every FUTURE append — post-forgery telemetry (lock_violation etc.) is the point.
    The chain break AT the forgery stays visible to verify()."""
    ledger.append(tmp_path, entry_type="commit_pass", tree="t", evidence={}, env={})
    with ledger.ledger_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"type": "contract_closed", "evidence": {"disposition": "done"}}\n')
    ledger.append(tmp_path, entry_type="lock_violation", tree=None,
                  evidence={"action": "close"}, env={})
    entries = ledger.read(tmp_path)
    assert entries[-1]["type"] == "lock_violation"  # recording continued past the forgery
    assert ledger.verify(tmp_path).ok is False  # ...and the forgery is still chain-flagged
