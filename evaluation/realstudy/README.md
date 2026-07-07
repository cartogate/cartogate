# Real-repo, independent-oracle study (headline V3 / V4 / V7)

This is the **credible** half of the value study: it measures Cartogate on a real third-party
repo it didn't author, with ground truth from tools **independent of Cartogate**, so the scores
can't be gamed. (The self-authored `tests/value/fixtures/proj` is kept only as an illustrative
appendix.)

| Hypothesis | Independent oracle | What it answers |
|---|---|---|
| **V3** navigation | **pyright** LSP `textDocument/references` (a different impl from jedi) | Does `find_references` match a compiler-grade reference set better than grep? |
| **V7** test selection | **coverage.py** per-test contexts (runtime truth) | Do the tests `suggest_tests` names actually execute the symbol? |
| **V4** duplicate gate | **objective construction** (no oracle needed) | Does `check_duplicate` catch true duplicates without false-flagging different-arity / method names? |

## Requirements

- **Node** (for pyright). The harness auto-resolves `pyright-langserver` from the npm global
  root or the npx cache (priming it with `npx -y pyright` if needed). Override with
  `PYRIGHT_LANGSERVER_JS=/path/to/langserver.index.js`, or `npm i -g pyright`.
- The corpus's test deps. click 8.1.7 needs only `pytest` + `pytest-cov` (already in `[dev]`).

## Run

```bash
python -m evaluation.corpus.fetch_corpus            # read-only tarball, sha256-pinned
python -m evaluation.realstudy.run_realstudy --sample 25
python scripts/build_value_report.py                # fold results into the report + README
```

`--only v3|v4|v7` runs a subset. `--max-truth N` (V7) bounds the exercising-test count for a
symbol to be sampled (where selection is meaningful). Runtime is a few minutes (Cartogate index
~20s, the corpus test suite under coverage ~5s, pyright settle + ~25 reference queries).

## How it stays fair

- **Read-only corpus:** a source tarball is downloaded and extracted — no clone, no git, nothing
  that can push upstream (see `../corpus/CORPUS.md`).
- **Flat workspace:** `workspace.py` copies the package + tests into a temp dir as siblings so
  `import <pkg>` resolves within the project (qnames `click.*` / `tests.*`) and so pyright,
  coverage, and Cartogate all key on identical paths.
- **No circular oracle:** pyright (V3) and coverage (V7) are independent of jedi; V4's labels are
  objective by construction. Cartogate's honest sub-1.0 scores (V3 recall on re-exports, V7
  recall on dynamic dispatch) are reported, not hidden.
