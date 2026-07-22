from cartogate import audit_cli
from cartogate.audit import ledger


def test_verify_ok_returns_zero(tmp_path, monkeypatch, capsys):
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={}, env={})
    monkeypatch.chdir(tmp_path)
    assert audit_cli.main(["verify"]) == 0
    assert "intact" in capsys.readouterr().out.lower()


def test_verify_detects_tampering_returns_one(tmp_path, monkeypatch):
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={"n": 0}, env={})
    ledger.append(tmp_path, entry_type="write_block", tree=None, evidence={"n": 1}, env={})
    ledger.ledger_path(tmp_path).write_text(
        "\n".join(reversed(ledger._read_raw(tmp_path))) + "\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert audit_cli.main(["verify"]) == 1


def test_log_prints_entries(tmp_path, monkeypatch, capsys):
    ledger.append(tmp_path, entry_type="write_block", tree=None,
                  evidence={"signature": "def f()"}, env={})
    monkeypatch.chdir(tmp_path)
    assert audit_cli.main(["log"]) == 0
    assert "write_block" in capsys.readouterr().out
