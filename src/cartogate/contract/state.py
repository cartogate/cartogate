"""Active-contract file I/O — ``.cartogate/task.json`` (spec §4).

The working file is ephemeral (the ``.cartogate/`` state dir is self-ignoring); the audit
record is the LEDGER (declaration embeds the full contract + hash). One active contract per
repo in v1; redeclaring supersedes. A corrupt active file RAISES — the gate must block on it,
never silently skip enforcement.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from cartogate.contract.schema import Contract, ContractError, parse
from cartogate.daemon.discovery import STATE_DIR, ensure_state_dir

TASK_NAME = "task.json"


def task_path(repo: Path) -> Path:
    """Where the active contract lives (gitignored, alongside daemon state + ledger)."""
    return repo / STATE_DIR / TASK_NAME


def save(repo: Path, contract: Contract, lock_hash: str | None = None) -> None:
    """Write ``contract`` as the repo's active contract (supersedes any prior one).

    Atomic (temp file + ``os.replace``): a crash mid-write must not leave a torn task.json
    that fail-closed :func:`load` would then block on until a human deletes it (review M3).
    """
    ensure_state_dir(repo)
    path = task_path(repo)
    tmp = path.with_name(path.name + ".tmp")
    data: dict[str, object] = {"contract": contract.raw}
    if lock_hash is not None:
        data["lock_hash"] = lock_hash
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(repo: Path) -> Contract | None:
    """The active contract, ``None`` if none is declared.

    Raises :class:`ContractError` on a corrupt/invalid file — the caller (gate) must treat
    that as blocking, not as absence.
    """
    try:
        text = task_path(repo).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"active contract file is corrupt: {exc}") from exc
    if not isinstance(data, dict) or "contract" not in data:
        raise ContractError("active contract file is malformed (no 'contract' key)")
    return parse(data["contract"])


def clear(repo: Path) -> None:
    """Remove the active contract (idempotent)."""
    with contextlib.suppress(OSError):
        task_path(repo).unlink()


def lock_hash(repo: Path) -> str | None:
    """The active contract's lock hash (blake2b of the one-time token), or ``None``.

    None means unlocked, missing, or unreadable — NEVER raises: this is consulted on the
    tokenless ``close --abandon`` path, which must stay open no matter what (spec §2).
    """
    try:
        data = json.loads(task_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("lock_hash") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def stop_refusals(repo: Path) -> int:
    """The current stop-gate refusal counter for the active contract, or 0.

    Returns 0 if missing, corrupt, or no key — NEVER raises (spec §4).
    """
    try:
        data = json.loads(task_path(repo).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    value = data.get("stop_refusals") if isinstance(data, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


def bump_stop_refusals(repo: Path) -> int:
    """Atomically increment the stop-gate refusal counter, preserving other keys.

    Returns the new counter value. On corrupt file, returns current+1 WITHOUT
    persisting (the gate blocks corrupt contracts anyway; a fresh save() resets
    the counter naturally since it writes a new payload).
    """
    path = task_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt or missing: return 1 without rebuilding
        return 1
    if not isinstance(data, dict):
        return 1
    current = data.get("stop_refusals", 0)
    if not isinstance(current, int) or current < 0:
        current = 0
    new_count = current + 1
    data["stop_refusals"] = new_count
    # Atomic write via temp + os.replace, preserving all other keys (including lock_hash).
    # Distinct temp name from save()'s — two writers sharing one .tmp compound corruption
    # risk (review Medium, PR C).
    tmp = path.with_name(path.name + ".bump.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Persistence failed (disk full, transient lock) — the in-memory count still drives
        # THIS stop's refuse/ledger decision. A propagated exception would hit the stop-gate's
        # fail-open catch and buy a SILENT, unledgered allow (review High, PR C).
        pass
    return new_count
