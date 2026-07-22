# Verification contracts

A **contract** is a task-scoped, machine-checkable definition of done, declared *before* the
work. While a contract is active, the commit gate refuses "done" without the declared
evidence — and every decision is chained into the tamper-evident [audit ledger](./AUDIT.md).

```bash
cartogate task declare contract.json   # lint + activate (weak checks are REFUSED)
cartogate task status                  # evaluate now — exit 0 iff satisfied
cartogate task attest visual-signoff   # record a human sign-off, pinned to the staged tree
cartogate task close [--abandon]       # retire it (ledgered, never silent)
```

```jsonc
{
  "task": "wire the payment webhook",
  "checks": [                                   // exit-0 evidence — deterministic
    { "run": "python -m pytest tests/payments -v" },
    { "run": "python -m mypy src", "timeout": 600 }
  ],
  "attest": ["visual-signoff"],                 // human evidence — tree-pinned
  "scope": { "files": ["src/payments/*.py", "tests/payments/*.py"] }   // advisory
}
```

## Two evidence types

| | proves | enforced by |
|---|---|---|
| `run:` | the command exited 0 | executing it; exit code |
| `attest:` | a named human approved **this exact staged tree** | a ledger attestation whose tree hash equals the current `git write-tree` |

The judgment behind an attestation is human; the enforcement of *"judgment happened, on this
exact state"* is mechanical — **any change to the staged tree invalidates prior sign-offs by
construction**. Attach what was approved with `--artifact screenshot.png` (content-hashed into
the ledger). Attestor identity is asserted, not authenticated — same trust model as the ledger.

UI/UX work fits this way: most of it *is* exit-0 (build, types, axe-core, Playwright behavioral
assertions); the genuinely subjective residue ("looks right") is an `attest:` requirement, not a
fake metric. Model/LLM judgment can never satisfy a contract — it is advisory, always.

## Weak checks are refused at declaration

`declare` lints every `run:` command and **refuses** contracts whose checks cannot fail
(`exit 0`, `|| true`), are tautologies, or are silent on failure (quiet flags with no visible
output — the block message and the ledger depend on the diagnosis). Fix the contract, not the
gate; refusal before work starts costs nothing.

**Honest bound:** static lint cannot catch a *wrong-but-passing* check (one that accepts a
defective change). `cartogate stats` therefore flags checks that have **never failed** across
many runs — *unfalsified, not trustworthy* — for human review.

## What the gate does

With an active contract, a passing duplicate-gate commit then requires the contract to be
satisfied. On failure the commit is refused with the failing check's own output, pending
attestations with the exact command to run, and three remedies — fix the code, amend the
contract (`task declare`), or `task close --abandon`. **Every path is ledgered, none is
silent.** Corrupt contract state blocks (never silently skips); `close --abandon` works even
then. No active contract → the gate behaves exactly as before; contracts are fully opt-in.

Checks run in the **working directory** and evidence is stamped with the staged tree hash; if
your worktree diverges from the index the gate says so and records it. Stage everything before
committing for exact evidence.

**Scope is advisory (v1):** staged files outside `scope.files` produce one advisory line and a
`scope_deviation` ledger entry — never a block. Declare scope from the graph with
`cartogate task declare c.json --scope-from-symbol pkg.mod.symbol` (expands the symbol's file +
its blast radius; needs a resolved snapshot; an unresolvable symbol refuses the declaration).
Author `scope.files` with **forward slashes** (git's path style — matching is case-sensitive
and platform-independent), and note `*` matches across directory separators (`src/*.py` also
matches `src/sub/x.py`). `scope.symbols` written directly in the JSON is declaration metadata
only in v1 — it has no gate-time effect; use `--scope-from-symbol` to turn symbols into
enforced-scope files.

## Hygiene

- **Never embed secrets in `run:` commands** — the full contract, including check strings, is
  ledgered verbatim in plaintext (`.cartogate/` is gitignored, but treat it like CI logs).
- Checks are verbatim shell commands run by your platform shell — you own their portability,
  same as CI.
- A failing check's output tail is printed at commit time for diagnosis; don't have checks
  print secrets.

## Locked contracts, sealed checks, and the stop-gate

Three primitives turn a contract into a **termination gate** an outer loop (a driver, CI, or
you) can trust against the agent working under it:

**Locked contracts** — `declare --lock` prints a one-time token (Cartogate persists only its
hash). While locked: amending or closing `done` requires `--lock-token`; an authorized amend
*carries* the lock (`--lock` rotates it, token-gated `--unlock` drops it); and
**`close --abandon` never needs the token** — surrender is always available, always ledgered.
Wrong-token attempts become `lock_violation` ledger entries: tamper attempts are telemetry.
The **ledger walk is the lock authority** — hand-editing the state file cannot unlock, and a
forged ledger line cannot release or take over a lock (release requires disclosed-token proof,
verified against the stored hash). **Hold the token in the driver — never paste it into a
worker prompt.** Residual bound, stated honestly: total erasure of local state reads as "no
active contract" (never a false *done*) — a driver comparing its own record of the contract
hash against `status --json` catches the vanish.

**Sealed checks** — a second check set the working agent never iterates against. The driver
runs `cartogate task seal held.json` (same strict validation + weak-check lint as declare),
keeps the file **outside the repo**, and puts only `{"hash", "count"}` in the contract. At
termination, `cartogate task verify-sealed held.json` anchors the state to its ledger
declaration, verifies the bytes against the pinned hash (`sealed_mismatch` on any
substitution), runs the checks, and chains `sealed_pass`/`sealed_fail`. Exit codes: 0 pass,
1 fail/mismatch, 2 driver-side usage error. Sealed checks answer the measured failure mode
where agents saturate visible checks while failing held-out ones — and they are the
independent second author that bounds wrong-but-passing checks.

**The stop-gate** — `cartogate init --agent claude|devin` wires `cartogate-stop-gate` into the
harness's Stop hook. While a **locked** contract is unsatisfied (or its state has diverged),
the session may not quietly end: the stop is refused with the failing evidence and all three
remedies, **bounded** by the contract's `stop_budget` (default 3) so a stuck agent is never
ping-ponged forever — budget exhaustion allows the stop but chains `unsatisfied_stop`, which a
driver reads as "ended without done." The stop-gate is **fail-open** (a session-end hook must
never wedge your editor); the commit gate remains the fail-closed backstop. Unlocked contracts
never trigger it.

**The driver protocol** (any orchestrator — a human, CI, or a tool): declare locked (hold the
token) → run the agent → on its "done": `task status --json` (exit 0/1) then, if sealed,
`verify-sealed` (0/1/2) → pass: `close` with the token → fail: re-prompt with the evidence,
bounded, then escalate or surrender → abandon: `close --abandon`, tokenless, always. Every
state transition is a hash-chained ledger entry.

## What this is not

Contracts admit **symbol sets, file globs, commands, and attestation names — nothing else**.
"Improve robustness" is not a contract; prose goals stay in your docs. That restraint is the
point: everything a contract enforces is deterministic (exit codes, tree-hash equality, glob
membership), so a block is always something you can point at.
