from cartogate.audit import ledger


def test_canonical_is_order_independent():
    a = ledger._canonical({"b": 1, "a": 2})
    b = ledger._canonical({"a": 2, "b": 1})
    assert a == b == b'{"a":2,"b":1}'


def test_entry_hash_ignores_hash_field():
    e = {"seq": 0, "type": "commit_pass", "hash": "SHOULD_BE_IGNORED"}
    h1 = ledger._entry_hash(e)
    h2 = ledger._entry_hash({**e, "hash": "different"})
    assert h1 == h2
    assert len(h1) == 128


def test_decision_hash_is_reproducible_and_evidence_sensitive():
    ev = {"groups": [{"language": "python", "signature": "def f()"}]}
    h1 = ledger.decision_hash("commit_block", "abc123", ev)
    h2 = ledger.decision_hash("commit_block", "abc123", dict(ev))
    assert h1 == h2
    assert ledger.decision_hash("commit_block", "abc123", {"groups": []}) != h1
