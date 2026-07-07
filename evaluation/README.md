# Live agent A/B study (Layer 2 — V1: token usage)

This harness measures **what an agent spends to answer codebase questions with Cartogate
versus without it**. For each task it runs a real Claude tool-use loop twice:

- **with** — the six Cartogate tools (`check_duplicate`, `blast_radius`, `find_references`,
  `find_symbol`, `suggest_tests`, `doc_drift`) available;
- **without** — only generic `read_file` / `list_dir` / `grep` over the same corpus.

Token usage comes from the API `usage` field (exact). It is **opt-in** (needs the Anthropic
SDK + `ANTHROPIC_API_KEY`), never runs in CI, and its results are committed to
`evaluation/value_results.json` (key `V1`) so the study is retraceable.

## Run it

```bash
pip install -e ".[extract,mcp,evaluation]"   # the evaluation extra adds the anthropic SDK
export ANTHROPIC_API_KEY=sk-...

python -m evaluation.corpus.fetch_corpus      # fetch the pinned corpus snapshot (see corpus/CORPUS.md)
python -m evaluation.run_ab --trials 3 --model claude-sonnet-4-6
```

Point at any importable package with `--corpus path/to/pkg` (defaults to the fetched click
snapshot). Each task runs `--trials` times per arm; results report mean ± stdev.

## Cost & runtime

Roughly `tasks × arms × trials` short agent conversations (≈ a few hundred to a few thousand
tokens each). With the default 3 tasks × 2 arms × 3 trials that is ~18 conversations — cents,
not dollars, on a mid model. Model output is non-deterministic, so re-run for fresh draws.

## What's measured

| Field | Meaning |
|-------|---------|
| `overall_token_reduction_pct` | mean total tokens saved by the with-Cartogate arm |
| per-task `with/without_cartogate_tokens` | mean ± stdev total tokens (input+output) |
| per-task `*_turns_mean` | agent round-trips to an answer |
| per-task `with/without_correct` | trials whose `ANSWER:` line matched the graph truth |

Tasks and their targets are selected from the indexed corpus at runtime (`tasks.py`), so the
same set works on any package. Correctness uses Cartogate as the oracle, so the **headline is
token cost** (oracle-free); correctness is a secondary, labeled signal.
