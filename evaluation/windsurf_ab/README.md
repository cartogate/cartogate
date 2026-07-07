# Windsurf A/B task pack

Everything you need to run the manual Windsurf A/B tactical study from
[`docs/WINDSURF_AB_PROTOCOL.md`](./PROTOCOL.md). You drive Windsurf at the UI;
this pack fixes the codebase, the prompts, and the oracle so the result is reproducible.

| file | what it is |
|---|---|
| `taskpack/usersvc/` | the duplication-bait mini-codebase (known reusable symbols) |
| `SYMBOLS.md` | the starting symbol inventory (what "duplicate" means per task) |
| `TASKS.md` | the 5 prompts to paste into Cascade, verbatim |
| `tasks.json` | machine-readable prompts + categories + targets |
| `score.py` | the **oracle** — indexes a final tree, reports duplicates (primary metric) |
| `score.csv` | the scoring sheet (one row per trial) |
| `runs/` | drop each trial's `git diff` here (`<task>_<arm>_<trial>.diff`) |

## One-time setup

1. **Copy the codebase out to a scratch repo** so a Windsurf run doesn't mutate this repo, and so
   the gate indexes only the taskpack:
   ```bash
   cp -r evaluation/windsurf_ab/taskpack /tmp/usersvc-run && cd /tmp/usersvc-run
   git init -q && git add -A && git commit -qm baseline   # gives you a clean state to reset to
   ```
2. **Arm A wiring** (treatment) — in the scratch repo:
   - MCP: register `cartogate-mcp`, launched in `/tmp/usersvc-run` — it auto-detects that repo.
   - Rule: copy `.windsurf/rules/cartogate.md` from this repo.
   - Gate: copy `.windsurf/hooks.json` + point it at this repo's `hooks/windsurf_gate.py`. The gate
     auto-detects the repo from the edited file's `.git` (the `git init` in step 1 is what makes the
     scratch dir a detectable repo).
   - Warm daemon: `cartogate daemon start` in `/tmp/usersvc-run`.
   - **Verify the gate is live** (expect `Cartogate BLOCK … / exit=2`) — note the absolute
     `file_path` inside the scratch repo, which is what the gate auto-detects from:
     ```bash
     python /path/to/cartogate/hooks/windsurf_gate.py <<'EOF'
     {"tool_info": {"file_path": "/tmp/usersvc-run/x.py", "code": "def authenticate(name):\n    return 1\n"}}
     EOF
     echo "exit=$?"
     ```
3. **Arm B wiring** (control) — remove the MCP server, the rule file, and `.windsurf/hooks.json`.

## Per trial

Follow [§4 of the protocol](./PROTOCOL.md#4-per-trial-procedure). In short:
fresh Cascade conversation → paste one prompt from `TASKS.md` → let it finish → record the row in
`score.csv` and save the diff to `runs/`.

## Score the primary metric (no eyeballing)

After each trial, run the oracle over the final tree:

```bash
python -m evaluation.windsurf_ab.score /tmp/usersvc-run
# exit 0 + "clean" => no duplicate;  exit 1 + prints the signature => duplicate introduced
```

Fill `duplicate_introduced` from the exit code. Then reset for the next trial:

```bash
cd /tmp/usersvc-run && git checkout -- . && git clean -fdq
```

## Headline numbers (from `score.csv`)

- **Duplicate rate:** A vs B on the **reuse** tasks (the core result).
- **False-positive rate (A):** `gate_fired` on the **novel** tasks — must be ~0.
- **Efficiency:** mean steps (and tokens, if Windsurf exposes them) A vs B.
