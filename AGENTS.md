# Cartogate rules for coding agents

This repository runs **Cartogate**, a local deterministic code-knowledge gate over a typed graph
of the codebase (every function, class, call, import — across 11 languages). Reach the graph
through its MCP tools rather than guessing; they are deterministic and rest only on EXTRACTED
structural facts.

## The hard gate (can BLOCK — fix the issue, don't work around it)

- **Before creating a new function or class**, call `check_duplicate(signature)`. If it returns a
  hit, **reuse the existing symbol** instead of writing a new one. A `PreToolUse` (write-time) or
  git pre-commit block means a real duplicate on extracted evidence.

## Advisory tools (never block — use them to work safely)

**Impact & navigation**
- `blast_radius(symbol)` — what depends on a symbol (call before changing an exported/public one).
- `find_symbol(qualified_name)` / `find_references(qualified_name)` — navigate the graph.
- `impact_summary(symbols=[...] | diff=...)` — one report for a change: affected code + tests to
  run + docs to review.

**Change hygiene** (after editing a function/class)
- `suggest_tests(symbols=[...])` — the tests that exercise the changed symbols (run them).
- `doc_drift(symbols=[...])` — docs that reference the changed symbols and may be stale (review).
- `localize(test=..., diff=...)` — given a failing test, rank the likely culprit symbols from the
  change.
- `slice(source=..., line=...)` — program slice for a Python function: the statements that affect
  (or, with `forward`, are affected by) a given line.

**Code health** (advisory candidates to review)
- `find_cycles()` — import/dependency cycles.
- `find_duplicate_bodies()` — near-duplicate function bodies (copy-paste across renames).
- `find_dead_code()` — top-level symbols with no incoming reference.

Every block traces to a line of source. Anything uncertain or inferred is advisory and can never
block — so a refusal is always something you can point at in the code.


## Workspace (do this first)

Some editors (e.g. Windsurf) don't pass the open project to the MCP server, so Cartogate starts without knowing which repository you're in. On your FIRST Cartogate call of a session, include the `workspace_root` parameter (every Cartogate tool accepts it) set to the absolute path of THIS repository's root (the workspace folder open in your editor) — the call configures the workspace and runs in one step. If a tool ever returns `"action": "set_workspace"`, call `set_workspace` with `root` = that same path, then retry.

**On a BLOCK:** follow the ACTION line in the block message (usually: reuse the existing
symbol it names). Never retry the identical call, never rename the new symbol to evade
the gate, and never bypass with `--no-verify`.
