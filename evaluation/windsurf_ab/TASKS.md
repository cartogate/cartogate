# Task prompts (paste verbatim into Cascade)

Use **one fresh Cascade conversation per trial**. Paste the prompt exactly — do **not** add
"check for duplicates first" (the rule nudge is part of arm A's wiring, not the prompt). Run each
task **N ≥ 3 times per arm**.

The machine-readable version is [`tasks.json`](tasks.json); the reusable symbols are in
[`SYMBOLS.md`](SYMBOLS.md).

## Reuse tasks — the codebase already solves these (primary signal)

The right outcome is to **reuse** the existing symbol. A duplicate top-level signature in the final
tree = a miss (the oracle catches it).

1. **`auth`** — *target: `authenticate(name)` already in `usersvc/auth.py`*
   > Add a helper to usersvc that authenticates a user by name and returns whether they're valid.

2. **`validate`** — *target: `validate(record)` already in `usersvc/auth.py`*
   > I need a function in usersvc to validate a user record before saving it — it must require a non-empty name.

3. **`user`** — *target: `User` already in `usersvc/models.py`*
   > Add a User class to usersvc that stores a user's name.

## Novel-control tasks — the codebase does NOT solve these (false-positive signal)

The right outcome is to **write the new symbol**. The gate must **not** block these; if it does,
that's a false positive.

4. **`tax`** — *no such symbol exists*
   > Add a function compute_tax(amount, rate, region) to usersvc that returns the tax owed.

5. **`ledger`** — *no such symbol exists*
   > Add a Ledger class to usersvc that records transactions in a list.
