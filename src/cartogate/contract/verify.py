"""Contract evidence evaluation (spec §6): run checks, verify tree-pinned attestations.

Two evidence types, both deterministically enforceable: ``run:`` (a command's exit code) and
``attest:`` (a ledger attestation whose tree hash equals the CURRENT ``git write-tree`` — the
judgment is human; "judgment happened, on this exact state" is mechanical). v1 execution model:
checks run in the WORKING DIRECTORY, stamped with the staged write-tree; divergence between
worktree and index is surfaced, not silently ignored (spec §6.1/§10).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from cartogate.contract.schema import Check, Contract, contract_hash
from cartogate.gitio import run_git

_GIT_TIMEOUT_S = 10.0
_OUTPUT_CAP = 4000  # tail chars kept per check — enough to diagnose, bounded in the ledger


@dataclass(frozen=True)
class CheckResult:
    """One executed check. ``exit_code is None`` = timeout or spawn failure (still a failure)."""

    run: str
    exit_code: int | None
    output: str


@dataclass(frozen=True)
class ContractStatus:
    """Everything the gate needs: check results, attest satisfaction, tree, divergence."""

    checks: tuple[CheckResult, ...]
    attest: dict[str, bool]
    tree: str | None
    diverged: bool

    @property
    def ok(self) -> bool:
        """True iff every check exited 0 and every attestation is satisfied."""
        return all(r.exit_code == 0 for r in self.checks) and all(self.attest.values())


def current_tree(repo: Path) -> str | None:
    """The staged tree hash (``git write-tree``), or ``None`` when git can't answer."""
    out = run_git(["write-tree"], cwd=repo, timeout=_GIT_TIMEOUT_S)
    return out.decode("ascii", "replace").strip() if out is not None else None


def diverged(repo: Path) -> bool:
    """True when tracked files differ between working dir and index (checks read the worktree)."""
    out = run_git(["diff", "--name-only"], cwd=repo, timeout=_GIT_TIMEOUT_S)
    return bool(out is not None and out.strip())


def run_check_list(checks: tuple[Check, ...], repo: Path) -> tuple[CheckResult, ...]:
    """Execute every check in the tuple in the working directory, capturing combined output."""
    results: list[CheckResult] = []
    for check in checks:
        try:
            proc = subprocess.run(  # noqa: S602 — checks are user-declared shell commands by design
                check.run, shell=True, cwd=repo, capture_output=True, text=True,
                errors="replace",  # undecodable output must degrade, not vanish (review M1)
                timeout=check.timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            results.append(CheckResult(check.run, proc.returncode, output[-_OUTPUT_CAP:]))
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(check.run, None, f"timed out after {check.timeout:g}s")
            )
        except OSError as exc:
            # str(exc) can embed surrogate-escaped path bytes — sanitize so downstream strict
            # encodes never crash on this message (review Medium, PR B).
            msg = str(exc).encode("utf-8", "replace").decode("utf-8")
            results.append(CheckResult(check.run, None, f"could not run: {msg}"))
    return tuple(results)


def run_checks(contract: Contract, repo: Path) -> tuple[CheckResult, ...]:
    """Execute every ``run:`` check in the working directory, capturing combined output."""
    return run_check_list(contract.checks, repo)


def _proves(token: object, lock_hash_hex: object) -> bool:
    """True iff ``token`` is the preimage of ``lock_hash_hex`` — publicly verifiable proof of
    token knowledge (the token is disclosed only when the lock it guards is being retired)."""
    import hashlib

    return (
        isinstance(token, str) and isinstance(lock_hash_hex, str)
        and hashlib.blake2b(token.encode("utf-8")).hexdigest() == lock_hash_hex
    )


def _lock_walk(repo: Path) -> tuple[bool, dict[str, object] | None]:
    """``(declaration_open, evidence_of_the_governing_declaration)`` from the ledger.

    The ledger file itself sits in the worker's write surface, so **forged lines must not be
    honored** (re-verification 6b): while a LOCKED declaration is open, a ``contract_closed``
    releases it only with disposition ``abandoned*`` (the escape hatch — forging one is
    equivalent to exercising it: loud surrender, never a 'done') or evidence ``lock_token``
    proving the current lock; and a superseding ``contract_declared`` is honored only with a
    proving ``prior_token``. Anything else is IGNORED — the lock stays, and the next
    tokenless operation surfaces as a ``lock_violation``. Chain validation is deliberately
    NOT the auth mechanism (the hash algorithm is public; a forger can chain validly) — the
    token, which the worker never holds, is.
    """
    from cartogate.audit import ledger

    declared: dict[str, object] | None = None
    open_declaration = False
    for entry in ledger.read(repo):
        etype = entry.get("type")
        ev_raw = entry.get("evidence")
        ev: dict[str, object] = ev_raw if isinstance(ev_raw, dict) else {}
        current = declared.get("lock_hash") if (open_declaration and declared) else None
        if etype == "contract_declared":
            if isinstance(current, str) and not _proves(ev.get("prior_token"), current):
                continue  # forged takeover of a locked contract — not honored
            declared, open_declaration = ev, True
        elif etype == "contract_closed":
            if isinstance(current, str):
                disposition = str(ev.get("disposition", ""))
                if not disposition.startswith("abandoned") and not _proves(
                    ev.get("lock_token"), current
                ):
                    continue  # forged 'done' close of a locked contract — not honored
            declared, open_declaration = None, False
    return open_declaration, declared


def active_lock(repo: Path) -> str | None:
    """The lock hash governing the active contract (``None`` = unlocked / none / closed).

    The **ledger walk is the lock authority** (worker-writable ``task.json`` edits can't
    unlock — review Critical), and the walk only honors token-proofed releases (forged ledger
    lines can't either — re-verification 6b). The state file's hash remains a fallback for
    the rare best-effort-append miss; either surviving record keeps the lock sticky.
    """
    from cartogate.contract import state

    open_declaration, declared = _lock_walk(repo)
    value = declared.get("lock_hash") if (open_declaration and declared) else None
    ledger_lock = value if isinstance(value, str) else None
    return ledger_lock or state.lock_hash(repo)


def state_divergence(repo: Path, contract: Contract) -> str | None:
    """Why the live ``task.json`` diverges from its ledger declaration (``None`` = anchored).

    The state file is worker-writable; the token-proof ledger walk yields the governing
    declaration. The gate BLOCKS on divergence: a hand-edited contract or lock must never
    silently pass. A missing declaration also reads as divergence — the fail direction is
    refusal, and redeclaring repairs both records.
    """
    from cartogate.contract import state

    open_declaration, declared = _lock_walk(repo)
    if not open_declaration or declared is None:
        return "the active contract has no ledger declaration"
    if declared.get("contract_hash") != contract_hash(contract.raw):
        return "the active contract's content does not match its ledger declaration"
    if declared.get("lock_hash") != state.lock_hash(repo):
        return "the active contract's lock state does not match its ledger declaration"
    return None


def attest_status(contract: Contract, repo: Path) -> dict[str, bool]:
    """Per-name satisfaction: a ledger ``attestation`` for this contract pinned to the CURRENT
    tree. Any tree change invalidates prior sign-offs by construction — no staleness window."""
    from cartogate.audit import ledger

    tree = current_tree(repo)
    want = contract_hash(contract.raw)
    satisfied = dict.fromkeys(contract.attest, False)
    if tree is None:
        return satisfied
    for entry in ledger.read(repo):
        if entry.get("type") != "attestation" or entry.get("tree") != tree:
            continue
        ev = entry.get("evidence", {})
        name = ev.get("name")
        if ev.get("contract_hash") == want and name in satisfied:
            satisfied[name] = True
    return satisfied


def evaluate(contract: Contract, repo: Path) -> ContractStatus:
    """Run everything once and compose the full status (used by ``task status`` and the gate)."""
    return ContractStatus(
        checks=run_checks(contract, repo),
        attest=attest_status(contract, repo),
        tree=current_tree(repo),
        diverged=diverged(repo),
    )
