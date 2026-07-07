# Windsurf A/B tactical run — protocol

A manual, human-in-the-loop A/B study to measure what Cartogate buys a **Windsurf (Cascade)**
session. You drive Windsurf at the UI; this protocol fixes the tasks, the two arms, and the scoring
so the result is reproducible and not a vibe.

**What we measure**
- **Primary — duplicate prevention:** does Cartogate stop the agent from writing a function/class
  that already exists? (the v0 success criterion). Plus the **false-positive rate** — clean,
  genuinely-novel writes the gate wrongly blocks.
- **Secondary — efficiency:** tokens and Cascade steps to finish each task, with vs. without
  Cartogate.

The primary metric is scored by a **deterministic oracle**, not by eye: run the repo's own
duplicate detector over the final tree (see [Scoring](#5-scoring)). Human judgement is only needed
for "did the task actually get done".

---

## 1. The two arms

| | Arm **A** (treatment) | Arm **B** (control) |
|---|---|---|
| MCP server (`cartogate-mcp`) | ✅ registered | ❌ removed |
| Rule nudge (`.windsurf/rules/cartogate.md`) | ✅ present | ❌ removed/renamed |
| Write-time gate (`.windsurf/hooks.json` → `hooks/windsurf_gate.py`) | ✅ active | ❌ removed |
| Warm daemon (`cartogate daemon start`) | ✅ running | n/a |

Arm A is the full **A + B + C** integration from [`INTEGRATIONS.md`](INTEGRATIONS.md#windsurf--a--b--c--write-time-gate-via-cascade-hooks).
Arm B is stock Windsurf with no Cartogate at all. Keep the model, Windsurf version, and repo
snapshot identical across arms — only the Cartogate wiring changes.

> **Verify the gate is live before arm A.** From inside the repo (the gate auto-detects it from the
> file path), pipe a known-duplicate payload through the adapter and confirm it exits 2:
> ```bash
> python hooks/windsurf_gate.py <<'EOF'
> {"tool_info": {"file_path": "elsewhere.py", "code": "def authenticate(name):\n    return 1\n"}}
> EOF
> echo "exit=$?"   # expect: Cartogate BLOCK ... / exit=2  (on a repo where `authenticate` exists)
> ```

---

## 2. The task pack

Each task is **duplication bait**: the codebase already contains a symbol that solves it, so the
*correct* move is to reuse — and a naive agent tends to write a second copy. Use a repo snapshot
where the target symbols are known (the repo's `tests/fixtures/sample_pkg`, or any indexed project
you keep a symbol inventory for).

Pick **6–10 tasks**, mixing two categories so the run measures both blocking *and* false positives:

**Reuse tasks (the gate SHOULD eventually lead to reuse — primary signal).** Example prompts:
- "Add a helper that authenticates a user by name and returns whether they're valid." *(an
  `authenticate` already exists)*
- "I need a function to validate a user record before saving." *(a `validate` already exists)*
- "Write a `User` class with a name field." *(a `User` already exists)*

**Novel-control tasks (the gate must NOT block — false-positive signal).** Example prompts:
- "Add a function `compute_tax(amount, rate, region)` that returns the tax owed." *(no such symbol)*
- "Add a `Ledger` class that records transactions." *(no such symbol)*

Write each prompt once, verbatim, and reuse it across both arms and all trials. Record the prompts
and the known target symbols in your run sheet so the oracle scoring is unambiguous.

**Trials.** Agents are non-deterministic — run **N ≥ 3 trials per task per arm** and report rates,
not single runs.

---

## 3. Controlling for bias

- **Fresh conversation per trial** (no carry-over context between tasks).
- **Counterbalance arm order** — e.g. for each task run A,B,B,A rather than all-A-then-all-B, so a
  warming repo cache or your own learning doesn't favour one arm.
- **Same prompt text** in both arms — do *not* add "remember to check for duplicates" in arm A;
  the rule nudge is part of arm A's wiring, not the prompt.
- **Reset the working tree** (`git stash -u` / `git checkout -- .`) between trials so each starts
  from the same snapshot.

---

## 4. Per-trial procedure

For each (task, arm, trial):

1. Reset the repo to the snapshot; confirm `git status` clean.
2. (Arm A only) confirm the daemon is warm and the gate probe above exits 2.
3. Open a **fresh** Cascade conversation; paste the task prompt verbatim; let Cascade run to
   completion (it applies edits; in arm A the gate may block a write — let Cascade react naturally).
4. Record into the run sheet:
   - **steps** — number of Cascade turns/steps to finish,
   - **tokens** — from Windsurf's usage readout if available; else leave blank and rely on steps,
   - **gate_fired** (arm A) — did `windsurf_gate.py` block at least one write? (Cascade surfaces the
     block; the adapter also prints `Cartogate BLOCK …` to stderr),
   - **task_done** — did the final state accomplish the request? (human yes/no),
   - keep the **final diff** (`git diff > runs/<task>_<arm>_<trial>.diff`).
5. Reset the tree for the next trial.

---

## 5. Scoring

**Primary — duplicate introduced? (deterministic oracle, no eyeballing).** Run the repo's own
duplicate detector over the *final* tree of each trial — the same check the pre-commit gate uses:

```bash
python hooks/pre_commit.py <repo-path>   # exit 1 + prints the dup => a duplicate was introduced
```

A trial **introduced a duplicate** iff this exits non-zero (and names a signature the task's target
symbol already had). Tally per arm:

- **duplicate rate** = trials that introduced a duplicate ÷ total trials *(reuse tasks)*.
- **gate-block-then-reuse** (arm A) = trials where `gate_fired` and the final tree has **no**
  duplicate — the gate did its job and the agent reused.
- **false-positive rate** (arm A) = novel-control trials where `gate_fired` *(the gate blocked a
  legitimately novel write)* — should be **0**.

**Secondary — efficiency.** Mean ± stdev of **steps** (and **tokens** where captured) per arm, over
all trials. Report the reuse tasks and novel tasks separately (reuse tasks are where Cartogate
should change behaviour).

### Scoring sheet (one row per trial)

| task | category | arm | trial | steps | tokens | gate_fired | duplicate_introduced | task_done |
|------|----------|-----|-------|-------|--------|------------|----------------------|-----------|
| auth | reuse | A | 1 | | | | | |
| auth | reuse | B | 1 | | | n/a | | |
| … | | | | | | | | |

### Headline numbers to report

- **Duplicate rate:** A `__%` vs B `__%` (reuse tasks) — the core result.
- **False-positive rate (A):** `__%` (novel tasks) — must stay ~0 for the gate to be trustworthy.
- **Efficiency:** mean steps/tokens A vs B (note direction; the gate may *add* a step when it
  blocks-then-reuses — that's an acceptable cost for avoiding a duplicate).

---

## 6. Readiness checklist

- [ ] `hooks/windsurf_gate.py` present; gate probe exits 2 on a known duplicate, 0 on a novel symbol.
- [ ] `.windsurf/hooks.json` registers `pre_write_code` → `python hooks/windsurf_gate.py`.
- [ ] MCP server reachable from Windsurf (arm A); rule file present (arm A).
- [ ] Repo snapshot + symbol inventory fixed; task prompts written verbatim.
- [ ] Daemon warm for arm A.
- [ ] Run sheet / `runs/` dir ready to capture diffs.

When every box is checked, the run is reproducible and the primary metric is oracle-scored.
```

> **Note on capturing tokens.** Windsurf does not always expose a per-conversation token count. If
> it doesn't, drop the token column and report **steps** as the efficiency proxy — the duplicate
> metric (the headline) does not depend on token capture.
