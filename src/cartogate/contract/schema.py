"""Verification-contract schema: parse, validate, hash (spec §3–§4).

A contract is a per-task, machine-checkable declaration: ``run:`` checks (exit-0 evidence),
``attest:`` names (human sign-offs pinned to tree hashes), and an advisory ``scope``. Validation
REFUSES malformed input (unknown keys, empty evidence) — refusal at declaration costs nothing
and is always actionable (spec §5, scrimp check-defective evidence).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from cartogate.hashing import canonical_blake2b

DEFAULT_TIMEOUT_S = 300.0
_MAX_TIMEOUT_S = 3600.0  # the timeout is the ONE safety bound around shell execution — cap it
_MAX_STOP_BUDGET = 100
_TOP_KEYS = {"task", "checks", "attest", "scope", "sealed", "stop_budget"}
_CHECK_KEYS = {"run", "timeout"}
_SCOPE_KEYS = {"files", "symbols"}
_SEALED_KEYS = {"hash", "count"}


class ContractError(ValueError):
    """A malformed contract — refused at declaration, blocked at the gate."""


@dataclass(frozen=True)
class Check:
    """One exit-0 verification command."""

    run: str
    timeout: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Contract:
    """A validated contract. ``raw`` is the exact input dict — ledger-embedded and hashed."""

    task: str
    checks: tuple[Check, ...]
    attest: tuple[str, ...]
    scope_files: tuple[str, ...]
    scope_symbols: tuple[str, ...]
    raw: dict[str, Any]
    sealed_hash: str | None = None
    sealed_count: int = 0
    stop_budget: int = 3


def contract_hash(raw: dict[str, Any]) -> str:
    """blake2b hex over canonical JSON (sorted keys, no whitespace) — key-order-free."""
    return canonical_blake2b(raw)


def _str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(x, str) and x.strip() for x in value
    ):
        raise ContractError(f"{where} must be a list of non-empty strings")
    return tuple(value)


def parse_check_list(data: object, where: str = "checks") -> tuple[Check, ...]:
    """Validate a raw list into :class:`Check` tuple — the ONE check-list validator.

    Shared by contract ``checks`` and sealed files (review PR B: two hand-rolled, weaker
    reimplementations let a string timeout silently DROP a held-out check and let an
    uncapped/NaN/Infinity timeout crash or hang verify-sealed). Strictness identical
    everywhere: unknown keys refused, timeout NaN-immune and capped.
    """
    if not isinstance(data, list):
        raise ContractError(f"{where} must be a JSON list")
    checks: list[Check] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ContractError(f"{where}[{i}] must be an object")
        bad = set(item) - _CHECK_KEYS
        if bad:
            raise ContractError(f"{where}[{i}] unknown key(s): {', '.join(sorted(bad))}")
        run = item.get("run")
        if not isinstance(run, str) or not run.strip():
            raise ContractError(f"{where}[{i}].run must be a non-empty command string")
        timeout = item.get("timeout", DEFAULT_TIMEOUT_S)
        # Review H1: json accepts NaN/Infinity literals and bool is an int subclass. The
        # range check is NaN-immune (0 < nan is False) and the cap rejects Infinity — a
        # check must never crash subprocess.run or hang unbounded.
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not (0 < timeout <= _MAX_TIMEOUT_S)
        ):
            raise ContractError(
                f"{where}[{i}].timeout must be a number in (0, {_MAX_TIMEOUT_S:g}]"
            )
        checks.append(Check(run=run, timeout=float(timeout)))
    return tuple(checks)


def parse(data: object) -> Contract:
    """Validate ``data`` into a :class:`Contract`; raise :class:`ContractError` on any defect."""
    if not isinstance(data, dict):
        raise ContractError("contract must be a JSON object")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ContractError(f"unknown key(s): {', '.join(sorted(unknown))} — refuse, don't guess")
    task = data.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ContractError("'task' must be a non-empty string label")
    checks = list(parse_check_list(data.get("checks", []), where="checks"))
    attest = _str_list(data.get("attest", []), "'attest'")
    if len(set(attest)) != len(attest):
        raise ContractError("'attest' names must be unique")
    scope = data.get("scope", {})
    if not isinstance(scope, dict):
        raise ContractError("'scope' must be an object")
    bad_scope = set(scope) - _SCOPE_KEYS
    if bad_scope:
        raise ContractError(f"scope unknown key(s): {', '.join(sorted(bad_scope))}")
    scope_files = _str_list(scope.get("files", []), "scope.files")
    scope_symbols = _str_list(scope.get("symbols", []), "scope.symbols")
    sealed_hash: str | None = None
    sealed_count: int = 0
    sealed = data.get("sealed")
    if sealed is not None:
        if not isinstance(sealed, dict):
            raise ContractError("'sealed' must be an object")
        bad_sealed = set(sealed) - _SEALED_KEYS
        if bad_sealed:
            raise ContractError(f"sealed unknown key(s): {', '.join(sorted(bad_sealed))}")
        seal_hash = sealed.get("hash")
        if not isinstance(seal_hash, str) or not re.fullmatch(r"^[0-9a-f]{128}$", seal_hash):
            raise ContractError("sealed.hash must be a 128-char lowercase hex string")
        sealed_hash = seal_hash
        seal_count = sealed.get("count")
        if not isinstance(seal_count, int) or seal_count <= 0:
            raise ContractError("sealed.count must be a positive integer")
        sealed_count = seal_count
    stop_budget: int = 3
    stop_budget_val = data.get("stop_budget", 3)
    if (
        not isinstance(stop_budget_val, int)
        or isinstance(stop_budget_val, bool)
        or not (0 < stop_budget_val <= _MAX_STOP_BUDGET)
    ):
        raise ContractError(
            f"stop_budget must be a positive int in (0, {_MAX_STOP_BUDGET}]"
        )
    stop_budget = stop_budget_val
    if not checks and not attest:
        raise ContractError(
            "a contract needs at least one evidence requirement (checks or attest) — "
            "it enforces nothing otherwise"
        )
    return Contract(
        task=task, checks=tuple(checks), attest=attest,
        scope_files=scope_files, scope_symbols=scope_symbols, raw=dict(data),
        sealed_hash=sealed_hash, sealed_count=sealed_count, stop_budget=stop_budget,
    )


def loads(text: str) -> Contract:
    """Parse contract JSON text; :class:`ContractError` on bad JSON or bad shape."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"contract is not valid JSON: {exc}") from exc
    return parse(data)
