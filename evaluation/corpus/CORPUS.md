# Value-study corpora

The value study runs against **real third-party libraries**, not Cartogate's own source, so the
numbers reflect unfamiliar codebases an agent must navigate. Two corpora are pinned in the registry
(`CORPORA` in `fetch_corpus.py`) so the headline results aren't specific to one repo or one style
of codebase — a CLI application and a plain library:

| Field | click (default / headline) | jmespath (second corpus) |
|-------|----------------------------|--------------------------|
| Repository | [pallets/click](https://github.com/pallets/click) | [jmespath/jmespath.py](https://github.com/jmespath/jmespath.py) |
| Release pinned | tag `8.1.7` | tag `1.0.1` |
| Kind | CLI application | library |
| License | BSD-3-Clause | MIT |
| Archive sha256 | `CORPUS.click.lock` | `CORPUS.jmespath.lock` |
| Why chosen | Mid-size OOP Python with `docs/` + a fast **hermetic** test suite (the coverage oracle needs it). Its commands dispatch dynamically (`runner.invoke`), which makes it a hard case for static test-selection. | A query library with direct unit tests over its lexer/parser/functions — a non-CLI counterpoint. (It still routes through a `search()`/visitor facade, so V7 recall is again dispatch-limited — a useful confirmation that the ceiling is general.) |

Each corpus's archive is sha256-pinned in its own `CORPUS.<name>.lock` (created on first fetch).

## Strictly read-only

The fetch **downloads and extracts a source tarball over HTTPS** — there is no `git clone`,
no remote, and no git history in the snapshot, so nothing can ever push to the upstream repo.
The snapshot dir is gitignored, so it also cannot be staged into a Cartogate commit.

## Reproducing the snapshot

```bash
python -m evaluation.corpus.fetch_corpus            # click (default)
python -m evaluation.corpus.fetch_corpus jmespath   # the second corpus
```

Each snapshot lands in `evaluation/corpus/_snapshot/<name>/` (gitignored — not committed). The
fetch verifies the archive's **sha256** against `CORPUS.<name>.lock`; a mismatch fails loudly so a
corpus can never silently drift. To re-pin to a new release, change the corpus's `tag` in the
`CORPORA` registry, delete its lock file, and re-fetch. To add a corpus, add a `Corpus(...)` entry.

## Note on vendoring

We fetch-by-tarball-with-sha256-lock rather than committing the files to keep the Cartogate
repo light. For a fully offline run, copy a corpus's `_snapshot/<name>/<package>` somewhere and
point `--corpus` at it directly — the harness only needs a path to an importable package.
