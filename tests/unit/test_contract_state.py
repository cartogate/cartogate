"""Active-contract file — .cartogate/task.json (ephemeral; the LEDGER is the audit record)."""
from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.contract import state
from cartogate.contract.schema import ContractError, contract_hash, parse


def _contract():  # type: ignore[no-untyped-def]
    return parse({"task": "t", "checks": [{"run": "pytest -v tests/"}]})


def test_roundtrip(tmp_path: Path) -> None:
    c = _contract()
    state.save(tmp_path, c)
    loaded = state.load(tmp_path)
    assert loaded is not None and loaded.raw == c.raw
    assert contract_hash(loaded.raw) == contract_hash(c.raw)


def test_missing_is_none(tmp_path: Path) -> None:
    assert state.load(tmp_path) is None


def test_corrupt_file_raises(tmp_path: Path) -> None:
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    with pytest.raises(ContractError):
        state.load(tmp_path)  # corrupt ACTIVE contract is never silently ignored (gate blocks)


def test_invalid_contract_content_raises(tmp_path: Path) -> None:
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text('{"contract": {"task": ""}}', encoding="utf-8")
    with pytest.raises(ContractError):
        state.load(tmp_path)


def test_clear_removes_and_is_idempotent(tmp_path: Path) -> None:
    state.save(tmp_path, _contract())
    state.clear(tmp_path)
    assert state.load(tmp_path) is None
    state.clear(tmp_path)  # no error on second clear


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    """Review M3: save goes through temp-file + os.replace so a crash mid-write can't leave
    a torn task.json that fail-closed load() then blocks on forever."""
    state.save(tmp_path, _contract())
    state.save(tmp_path, _contract())  # overwrite is fine
    siblings = sorted(p.name for p in state.task_path(tmp_path).parent.glob("task.json*"))
    assert siblings == ["task.json"]


def test_lock_hash_roundtrip_and_default(tmp_path: Path) -> None:
    state.save(tmp_path, _contract())
    assert state.lock_hash(tmp_path) is None  # unlocked default — backward compatible
    state.save(tmp_path, _contract(), lock_hash="ab" * 64)
    assert state.lock_hash(tmp_path) == "ab" * 64
    loaded = state.load(tmp_path)
    assert loaded is not None  # load() unchanged by the lock field


def test_lock_hash_is_none_when_missing_or_corrupt(tmp_path: Path) -> None:
    assert state.lock_hash(tmp_path) is None
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert state.lock_hash(tmp_path) is None  # never raises — abandon path must stay open


def test_stop_refusals_starts_at_zero(tmp_path: Path) -> None:
    """stop_refusals returns 0 for a missing contract."""
    assert state.stop_refusals(tmp_path) == 0


def test_stop_refusals_is_zero_when_missing_or_corrupt(tmp_path: Path) -> None:
    """stop_refusals never raises — returns 0 for missing/corrupt."""
    assert state.stop_refusals(tmp_path) == 0
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert state.stop_refusals(tmp_path) == 0


def test_bump_stop_refusals_increments_and_persists(tmp_path: Path) -> None:
    """bump_stop_refusals increments atomically and preserves other keys."""
    state.save(tmp_path, _contract(), lock_hash="ab" * 64)
    assert state.bump_stop_refusals(tmp_path) == 1
    assert state.bump_stop_refusals(tmp_path) == 2
    assert state.bump_stop_refusals(tmp_path) == 3
    # Verify it persists across load cycles
    loaded_hash = state.lock_hash(tmp_path)
    assert loaded_hash == "ab" * 64  # lock_hash preserved
    assert state.stop_refusals(tmp_path) == 3


def test_bump_stop_refusals_resets_on_new_save(tmp_path: Path) -> None:
    """bump_stop_refusals increments, but save() writes a fresh payload (no stop_refusals key)."""
    state.save(tmp_path, _contract())
    state.bump_stop_refusals(tmp_path)
    state.bump_stop_refusals(tmp_path)
    assert state.stop_refusals(tmp_path) == 2
    # Redeclare (new save) — counter resets naturally
    state.save(tmp_path, _contract())
    assert state.stop_refusals(tmp_path) == 0


def test_bump_stop_refusals_corrupt_returns_without_persisting(tmp_path: Path) -> None:
    """On corrupt file, bump returns 1 without rebuilding the file."""
    state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
    n = state.bump_stop_refusals(tmp_path)
    assert n == 1
    # File remains corrupt (not rebuilt)
    with pytest.raises(ContractError):
        state.load(tmp_path)


def test_bump_survives_a_failed_persist_with_in_memory_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review High (PR C): a failed counter WRITE must not propagate — the in-memory count
    still drives this stop's refuse/ledger decision, so an I/O hiccup can never buy a silent,
    unledgered allow."""
    import os as _os

    state.save(tmp_path, _contract())
    assert state.bump_stop_refusals(tmp_path) == 1

    def _boom(*a: object, **k: object) -> None:
        raise OSError("disk says no")

    monkeypatch.setattr(_os, "replace", _boom)
    assert state.bump_stop_refusals(tmp_path) == 2  # increments in memory, never raises
