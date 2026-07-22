
from cartogate.audit import ledger


def test_actor_reads_agent_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "_git_ident", lambda repo: "Alex <a@x.com>")
    actor = ledger.resolve_actor(tmp_path, {"CARTOGATE_ACTOR": "claude-code"})
    assert actor["git"] == "Alex <a@x.com>"
    assert actor["agent"] == "claude-code"
    assert actor["src"] == "CARTOGATE_ACTOR"
    assert isinstance(actor["os"], str) and actor["os"]


def test_actor_missing_pieces_are_null_and_never_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "_git_ident", lambda repo: None)
    actor = ledger.resolve_actor(tmp_path, {})
    assert actor["git"] is None
    assert actor["agent"] is None
    assert actor["src"] is None


def test_git_ident_trims_timestamp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ledger,
        "run_git",
        lambda *a, **k: b"Alex Moulton <a@x.com> 1752600000 +0000\n",
    )
    assert ledger._git_ident(tmp_path) == "Alex Moulton <a@x.com>"
