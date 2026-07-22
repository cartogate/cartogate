"""Stop-gate: vendor-neutral, bounded session-end refusal for locked contracts (spec §4).

This console command is wired by ``cartogate init`` into the harness Stop hook (Claude Code,
Devin CLI). It is FAIL-OPEN: a session-end hook must never wedge the harness. The COMMIT gate
is the fail-closed backstop. Every error case (missing repo, corrupt state, parse failures)
returns 0 (allow the stop) and never raises.

A locked contract that is unsatisfied — or whose state has diverged from its ledger
declaration (tampering must not buy a quiet exit) — refuses the stop (exit 2), bounded by a
per-contract refusal budget (default 3). Budget exhausted → allow the stop but ledger
``unsatisfied_stop`` so the driver knows the session ended without meeting its own definition
of done.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cartogate.audit import ledger
from cartogate.contract import state, verify
from cartogate.contract.schema import Contract, contract_hash
from cartogate.surfaces import find_repo_root


def _refusal_reasons(
    contract: Contract, status: verify.ContractStatus, divergence: str | None
) -> str:
    """The evidence lines for a refusal — divergence first, then failing checks, then
    pending attestations. Sanitized: exotic check output must degrade, never raise."""
    reasons: list[str] = []
    if divergence is not None:
        reasons.append(f"state divergence: {divergence}")
    for result in status.checks:
        if result.exit_code != 0:
            tail = (result.output or "").split("\n")[-5:]
            reasons.extend(line for line in tail if line.strip())
    for name, satisfied in status.attest.items():
        if not satisfied:
            reasons.append(f"attest: {name} (pending — `cartogate task attest {name}`)")
    return "\n".join(reasons[:10]).encode("utf-8", "replace").decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    """Evaluate the locked contract at session end; refuse unsatisfied stops per budget.

    Reads the Stop-hook payload on stdin (garbage tolerated). Returns 0 (allow) or 2
    (refuse). Never raises — fail-open, the commit gate is the fail-closed backstop.
    """
    try:
        stdin_text = sys.stdin.read()
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        cwd_val = payload.get("cwd")
        repo = find_repo_root(Path(cwd_val) if isinstance(cwd_val, str) else Path.cwd())
        if repo is None:
            return 0  # not a repo — nothing to gate
        try:
            contract = state.load(repo)
        except Exception:  # noqa: BLE001 — corrupt state: the COMMIT gate blocks it; allow
            return 0
        if contract is None or verify.active_lock(repo) is None:
            return 0  # no contract / unlocked — the stop-gate only guards LOCKED contracts
        divergence = verify.state_divergence(repo, contract)
        status = verify.evaluate(contract, repo)
        if divergence is None and status.ok:
            return 0  # satisfied, anchored — a clean exit
        n = state.bump_stop_refusals(repo)
        if n > contract.stop_budget:
            # Bounded refusal: an unbounded stop-gate would ping-pong a stuck agent forever.
            # Allow the stop, but LOUDLY — the driver reads this as "ended without done".
            ledger.append(repo, entry_type="unsatisfied_stop", tree=None,
                          evidence={"contract_hash": contract_hash(contract.raw),
                                    "refusals": n - 1})
            return 0
        ledger.append(repo, entry_type="stop_refused", tree=None,
                      evidence={"contract_hash": contract_hash(contract.raw),
                                "refusal": n, "budget": contract.stop_budget})
        print(
            f"session stop refused: locked contract {contract.task!r} is not satisfied "
            f"(refusal {n}/{contract.stop_budget})\n\n"
            f"{_refusal_reasons(contract, status, divergence)}\n\n"
            "Remedies:\n"
            "  1. Make the declared checks pass (this is the declared definition of done)\n"
            "  2. Amend the contract (`cartogate task declare <file> --lock-token <token>`)\n"
            "  3. Surrender: `cartogate task close --abandon` (always available, ledgered)",
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 — fail-open: never wedge the harness at session end
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
