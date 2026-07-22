"""`cartogate task` — declare (lint-refused when weak), attest, status, close (spec §4-§5)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.conftest import git_cmd as _git
from tests.conftest import init_git_repo as _init
from tests.conftest import write_contract as _write_contract

from cartogate.audit import ledger
from cartogate.task_cli import main as task_main

_PY = f'"{sys.executable}"'


# Shared helper — the duplicate gate blocked a second local copy (2026-07-18).


def test_declare_writes_state_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    assert task_main(["declare", str(p)]) == 0
    from cartogate.contract import state

    assert state.load(tmp_path) is not None
    entries = ledger.read(tmp_path)
    assert entries and entries[-1]["type"] == "contract_declared"
    assert entries[-1]["evidence"]["contract"]["task"] == "t"


def test_declare_refuses_weak_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": "exit 0"}]})
    assert task_main(["declare", str(p)]) == 1
    assert "cannot fail" in capsys.readouterr().err
    from cartogate.contract import state

    assert state.load(tmp_path) is None  # refused = NOT declared


def test_declare_refuses_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "contract.json"
    p.write_text("{broken", encoding="utf-8")
    assert task_main(["declare", str(p)]) == 1
    assert "JSON" in capsys.readouterr().err


def test_attest_records_tree_pinned_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    p = _write_contract(tmp_path, {"task": "t", "attest": ["visual"]})
    assert task_main(["declare", str(p)]) == 0
    art = tmp_path / "shot.png"
    art.write_bytes(b"png-bytes")
    assert task_main(["attest", "visual", "--artifact", str(art)]) == 0
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "attestation" and entry["tree"]
    assert entry["evidence"]["name"] == "visual"
    assert list(entry["evidence"]["artifacts"].values())[0]  # blake2b hex recorded


def test_attest_unknown_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "attest": ["visual"]})
    task_main(["declare", str(p)])
    assert task_main(["attest", "nope"]) == 1
    assert "not declared" in capsys.readouterr().err


def test_status_reports_and_exits_by_satisfaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(
        tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "import sys; sys.exit(1)"'}]}
    )
    task_main(["declare", str(p)])
    assert task_main(["status"]) == 1  # unsatisfied -> nonzero (agents can poll it)
    out = capsys.readouterr().out
    assert "FAIL" in out
    p2 = _write_contract(tmp_path, {"task": "t2", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p2)])  # redeclare supersedes
    assert task_main(["status"]) == 0


def test_close_clears_and_ledgers_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "attest": ["visual"]})
    task_main(["declare", str(p)])
    assert task_main(["close", "--abandon"]) == 0
    from cartogate.contract import state

    assert state.load(tmp_path) is None
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "contract_closed"
    assert entry["evidence"]["disposition"] == "abandoned"  # the honest, attributable escape


def test_no_active_contract_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert task_main(["status"]) == 1
    assert task_main(["attest", "x"]) == 1
    assert task_main(["close"]) == 1
    assert "no active contract" in capsys.readouterr().err


def test_declare_handles_non_utf8_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review M2: UnicodeDecodeError is a ValueError, not OSError — must hit the clean
    error path, not a raw traceback."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "c.json"
    p.write_bytes(b"\xff\xfe not utf8")
    assert task_main(["declare", str(p)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_close_abandon_works_on_a_corrupt_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review High (PR B): the gate's corrupt-contract block message names `close --abandon`
    as a remedy — it must actually work: clear the file and ledger the disposition."""
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert task_main(["close", "--abandon"]) == 0
    assert state.load(tmp_path) is None  # file gone — the gate unblocks
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "contract_closed"
    assert "corrupt" in entry["evidence"]["disposition"]  # attributable, never silent
    # A plain close (no --abandon) still refuses — nothing parseable to close cleanly.
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert task_main(["close"]) == 1


def test_declare_lock_prints_token_once_and_stores_only_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib as _hl

    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    assert task_main(["declare", str(p), "--lock"]) == 0
    out = capsys.readouterr().out
    token = out.split("):", 1)[1].split()[0].strip()  # printed once after the label
    assert len(token) == 64 and state.lock_hash(tmp_path) == _hl.blake2b(
        token.encode()).hexdigest()
    assert token not in state.task_path(tmp_path).read_text(encoding="utf-8")  # hash only
    entry = ledger.read(tmp_path)[-1]
    assert entry["type"] == "contract_declared" and entry["evidence"]["locked"] is True


def test_locked_redeclare_and_close_require_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p), "--lock"])
    token = capsys.readouterr().out.split("):", 1)[1].split()[0].strip()
    # No token -> refused + lock_violation ledgered; contract unchanged.
    assert task_main(["declare", str(p)]) == 1
    assert "LOCKED" in capsys.readouterr().err
    assert ledger.read(tmp_path)[-1]["type"] == "lock_violation"
    assert task_main(["close"]) == 1
    assert ledger.read(tmp_path)[-1]["evidence"]["action"] == "close"
    # Wrong token -> refused too.
    assert task_main(["close", "--lock-token", "00" * 32]) == 1
    # Correct token -> close(done) succeeds.
    assert task_main(["close", "--lock-token", token]) == 0
    assert ledger.read(tmp_path)[-1]["evidence"]["disposition"] == "done"


def test_locked_abandon_is_tokenless_and_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "attest": ["visual"]})
    task_main(["declare", str(p), "--lock"])
    capsys.readouterr()
    assert task_main(["close", "--abandon"]) == 0  # the escape hatch NEVER needs the token
    assert state.load(tmp_path) is None
    assert ledger.read(tmp_path)[-1]["evidence"]["disposition"] == "abandoned (locked)"


def test_status_json_shape_and_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                                   "attest": ["visual"]})
    task_main(["declare", str(p)])
    capsys.readouterr()
    rc = task_main(["status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["ok"] is False  # attest pending
    assert data["task"] == "t" and data["locked"] is False and data["sealed"] is None
    assert data["checks"][0]["exit"] == 0 and data["attest"] == {"visual": False}
    assert "contract_hash" in data and "tree" in data and "diverged" in data


def test_stripping_lock_from_state_does_not_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Critical (PR A): task.json is worker-writable — the hash-chained LEDGER is the
    lock authority. Hand-stripping lock_hash must not unlock, and redeclaring over the
    tampered state must not launder the lock away. The abandon escape hatch stays open."""
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    assert task_main(["declare", str(p), "--lock"]) == 0
    capsys.readouterr()
    data = json.loads(state.task_path(tmp_path).read_text(encoding="utf-8"))
    data.pop("lock_hash")  # the tamper: one-line hand edit
    state.task_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert task_main(["close"]) == 1  # still locked per the ledger
    assert ledger.read(tmp_path)[-1]["type"] == "lock_violation"
    assert task_main(["declare", str(p)]) == 1  # laundering via tokenless redeclare refused
    assert ledger.read(tmp_path)[-1]["type"] == "lock_violation"
    assert task_main(["close", "--abandon"]) == 0  # escape hatch untouched


def test_amend_with_token_preserves_rotates_or_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review High (PR A): an authorized amend must NOT silently strip the lock — it carries
    by default, rotates with --lock, and drops only with an explicit --unlock."""
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p), "--lock"])
    token = capsys.readouterr().out.split("):", 1)[1].split()[0].strip()
    old = state.lock_hash(tmp_path)
    assert task_main(["declare", str(p), "--lock-token", token]) == 0  # amend: lock CARRIES
    capsys.readouterr()
    assert state.lock_hash(tmp_path) == old
    assert task_main(["close"]) == 1  # still token-gated
    capsys.readouterr()
    assert task_main(["declare", str(p), "--lock-token", token, "--lock"]) == 0  # rotate
    token2 = capsys.readouterr().out.split("):", 1)[1].split()[0].strip()
    assert state.lock_hash(tmp_path) not in (None, old)
    assert task_main(["declare", str(p), "--lock-token", token2, "--unlock"]) == 0  # explicit
    capsys.readouterr()
    assert state.lock_hash(tmp_path) is None
    assert task_main(["close"]) == 0  # genuinely unlocked now


def test_status_json_without_contract_still_emits_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review M3 (PR A): the machine probe must be machine-readable in EVERY state."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = task_main(["status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1 and data["ok"] is False and data["task"] is None


def test_forged_ledger_close_does_not_release_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-verification 6b: a hand-appended, unsigned contract_closed line must NOT unlock —
    releasing a lock requires PROOF of the token (or a loud abandon). Forgeries are ignored."""
    from cartogate.audit import ledger as _ledger
    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p), "--lock"])
    token = capsys.readouterr().out.split("):", 1)[1].split()[0].strip()
    with _ledger.ledger_path(tmp_path).open("a", encoding="utf-8") as fh:  # the forgery
        fh.write(json.dumps({"type": "contract_closed",
                             "evidence": {"contract_hash": "x", "disposition": "done"}}) + "\n")
    data = json.loads(state.task_path(tmp_path).read_text(encoding="utf-8"))
    data.pop("lock_hash", None)
    state.task_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert task_main(["close"]) == 1  # STILL locked — the forged release was ignored
    assert ledger.read(tmp_path)[-1]["type"] == "lock_violation"
    assert task_main(["close", "--lock-token", token]) == 0  # the real token still works
    closed = [e for e in ledger.read(tmp_path)
              if e["type"] == "contract_closed" and e["evidence"].get("lock_token")][-1]
    import hashlib as _hl
    # The authorized close DISCLOSES the one-time token as publicly-verifiable proof.
    assert _hl.blake2b(closed["evidence"]["lock_token"].encode()).hexdigest()


def test_forged_declaration_takeover_is_not_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-verification 6b-variant: a forged contract_declared carrying the WORKER'S own lock
    hash cannot take over — superseding a locked declaration requires prior-token proof."""
    import hashlib as _hl

    from cartogate.audit import ledger as _ledger

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p), "--lock"])
    capsys.readouterr()
    worker_token = "aa" * 32
    forged_hash = _hl.blake2b(worker_token.encode()).hexdigest()
    with _ledger.ledger_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "contract_declared",
                             "evidence": {"contract": {"task": "t"}, "contract_hash": "x",
                                          "locked": True, "lock_hash": forged_hash}}) + "\n")
    assert task_main(["close", "--lock-token", worker_token]) == 1  # takeover fails
    assert ledger.read(tmp_path)[-1]["type"] == "lock_violation"
    assert task_main(["close", "--abandon"]) == 0  # escape hatch untouched, as ever


def test_forged_abandon_is_equivalent_to_the_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Documented equivalence: forging an 'abandoned' close ≈ running the always-available
    abandon — it can only ever surrender LOUDLY, never yield a 'done'."""
    from cartogate.audit import ledger as _ledger

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p), "--lock"])
    capsys.readouterr()
    with _ledger.ledger_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "contract_closed",
                             "evidence": {"disposition": "abandoned (locked)"}}) + "\n")
    # Even STRONGER than equivalence: the state-file fallback keeps the lock sticky, so the
    # forgery achieves nothing — and the REAL escape hatch still works, loudly.
    assert task_main(["declare", str(p)]) == 1  # still refused (file fallback holds the lock)
    assert task_main(["close", "--abandon"]) == 0  # the genuine escape hatch, as ever


def test_seal_lints_and_prints_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib as _hl

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    good = tmp_path.parent / "held.json"  # OUTSIDE the repo — no warning expected
    good.write_text(json.dumps([{"run": "pytest -v tests/"}]), encoding="utf-8")
    assert task_main(["seal", str(good)]) == 0
    out = capsys.readouterr().out
    assert _hl.blake2b(good.read_bytes()).hexdigest() in out and '"count": 1' in out
    weak = tmp_path.parent / "weak.json"
    weak.write_text(json.dumps([{"run": "exit 0"}]), encoding="utf-8")
    assert task_main(["seal", str(weak)]) == 1
    assert "cannot fail" in capsys.readouterr().err
    inside = tmp_path / "held.json"
    inside.write_text(json.dumps([{"run": "pytest -v tests/"}]), encoding="utf-8")
    task_main(["seal", str(inside)])
    assert "INSIDE the repo" in capsys.readouterr().out  # custody warning


def test_verify_sealed_happy_and_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib as _hl

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    held = tmp_path.parent / "held2.json"
    held.write_text(json.dumps([{"run": f'{_PY} -c "print(1)"'}]), encoding="utf-8")
    h = _hl.blake2b(held.read_bytes()).hexdigest()
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                                   "sealed": {"hash": h, "count": 1}})
    task_main(["declare", str(p)])
    assert task_main(["verify-sealed", str(held)]) == 0
    assert ledger.read(tmp_path)[-1]["type"] == "sealed_pass"
    held.write_text(json.dumps([{"run": f'{_PY} -c "print(2)"'}]), encoding="utf-8")
    assert task_main(["verify-sealed", str(held)]) == 1  # substituted file
    assert ledger.read(tmp_path)[-1]["type"] == "sealed_mismatch"


def test_verify_sealed_fail_and_no_sealed_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib as _hl

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    held = tmp_path.parent / "held3.json"
    code = "import sys; print('held-out broke'); sys.exit(1)"
    held.write_text(json.dumps([{"run": f'{_PY} -c "{code}"'}]), encoding="utf-8")
    h = _hl.blake2b(held.read_bytes()).hexdigest()
    p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                                   "sealed": {"hash": h, "count": 1}})
    task_main(["declare", str(p)])
    assert task_main(["verify-sealed", str(held)]) == 1
    assert ledger.read(tmp_path)[-1]["type"] == "sealed_fail"
    assert "held-out broke" in capsys.readouterr().err
    p2 = _write_contract(tmp_path, {"task": "t2", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
    task_main(["declare", str(p2)])
    assert task_main(["verify-sealed", str(held)]) == 2  # no sealed block -> usage error


def test_verify_sealed_without_contract_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driver exit contract (spec §3): no active contract is a usage-level 2, not a check
    failure 1 — drivers branch on this distinction."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert task_main(["verify-sealed", "missing.json"]) == 2


def test_tampered_sealed_block_does_not_mint_sealed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Critical #1 (PR B): verify-sealed must anchor the worker-writable task.json to
    its ledger declaration — hand-editing the sealed block to a self-authored trivial file
    must refuse with state_divergence, never mint a sealed_pass."""
    import hashlib as _hl

    from cartogate.contract import state

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    real = tmp_path.parent / "real-held.json"
    real.write_text(json.dumps([{"run": f'{_PY} -c "import sys; sys.exit(1)"'}]),
                    encoding="utf-8")
    trivial = tmp_path.parent / "trivial.json"
    trivial.write_text(json.dumps([{"run": f'{_PY} -c "print(1)"'}]), encoding="utf-8")
    p = _write_contract(tmp_path, {
        "task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
        "sealed": {"hash": _hl.blake2b(real.read_bytes()).hexdigest(), "count": 1},
    })
    assert task_main(["declare", str(p)]) == 0
    data = json.loads(state.task_path(tmp_path).read_text(encoding="utf-8"))
    data["contract"]["sealed"] = {  # the tamper: point the contract at the trivial file
        "hash": _hl.blake2b(trivial.read_bytes()).hexdigest(), "count": 1}
    state.task_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert task_main(["verify-sealed", str(trivial)]) == 2
    types = [e["type"] for e in ledger.read(tmp_path)]
    assert "state_divergence" in types and "sealed_pass" not in types


def test_seal_refuses_malformed_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Critical #2 / High #3 / Low #6 (PR B): seal validates with the SAME strictness
    as schema.parse — string/uncapped timeouts and unknown keys refused; empty list refused."""
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    for bad, needle in (
        ([{"run": "x -v", "timeout": "30"}], "timeout"),
        ([{"run": "x -v", "timeout": 999999}], "timeout"),
        ([{"run": "x -v", "bogus": 1}], "bogus"),
        ([], "non-empty"),
    ):
        f = tmp_path.parent / "bad-seal.json"
        f.write_text(json.dumps(bad), encoding="utf-8")
        assert task_main(["seal", str(f)]) == 1
        assert needle in capsys.readouterr().err


def test_verify_sealed_refuses_malformed_file_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review Critical #2 (PR B): a malformed item must never be silently DROPPED at
    verify-time (a dropped held-out check = false sealed_pass) — strict reconstruction
    refuses the whole file, ledgered as sealed_mismatch."""
    import hashlib as _hl

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    bad = tmp_path.parent / "bad-held.json"
    bad.write_text(
        json.dumps([{"run": f'{_PY} -c "print(1)"'},
                    {"run": f'{_PY} -c "import sys; sys.exit(1)"', "timeout": "30"}]),
        encoding="utf-8",
    )
    p = _write_contract(tmp_path, {  # hash declared directly, bypassing seal's lint
        "task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
        "sealed": {"hash": _hl.blake2b(bad.read_bytes()).hexdigest(), "count": 2},
    })
    assert task_main(["declare", str(p)]) == 0
    assert task_main(["verify-sealed", str(bad)]) == 1
    types = [e["type"] for e in ledger.read(tmp_path)]
    assert "sealed_mismatch" in types and "sealed_pass" not in types


def test_verify_sealed_unreadable_file_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review M4 (PR B): a missing sealed-file path is a DRIVER-side usage error (2), not a
    check failure (1) — a driver must not re-prompt the worker for it."""
    import hashlib as _hl

    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = _write_contract(tmp_path, {
        "task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
        "sealed": {"hash": _hl.blake2b(b"whatever").hexdigest(), "count": 1},
    })
    assert task_main(["declare", str(p)]) == 0
    assert task_main(["verify-sealed", str(tmp_path.parent / "nope.json")]) == 2
