# Cartogate — CI/CD

## Overview

Every push to `main` and every pull request targeting `main` triggers the quality gate
defined in `.github/workflows/ci.yml`.  The gate is a matrix job: **ubuntu-latest** and
**windows-latest**, each running **Python 3.11 and 3.12**.  Cross-platform coverage is
intentional — the extractor and git-diff parser have Windows-specific code paths (path
normalisation, subprocess quoting) that must be exercised on a Windows runner.

`fail-fast: false` is set in the matrix so all four combinations report independently;
a single OS failure does not suppress results from the others.

---

## Pipeline stages

| Step | Command | Blocking |
|------|---------|---------|
| Install | `pip install -e ".[dev]"` | Yes |
| Lint | `ruff check .` | Yes |
| Type check | `mypy src` | Yes |
| Tests | `pytest` | Yes |
| Security scan | `pip-audit` | **No** (informational) |

### Install

```
pip install -e ".[dev]"
```

Installs the package in editable mode with every optional-dependency group:

- `dev` — ruff, mypy, pytest, pytest-benchmark, hypothesis, pip-audit, freezegun
- `extract` — tree-sitter, tree-sitter-python, jedi, python-lsp-server
- `mcp` — mcp SDK (pinned `>=1.2,<2`)

pip's download cache is keyed on `pyproject.toml` via `actions/setup-python@v5`.
Changing any dependency invalidates the cache automatically.

### Lint — `ruff check .`

Enforces pycodestyle, pyflakes, isort, pep8-naming, pyupgrade, bugbear, and
comprehension rules (ruff rule sets E, F, I, N, UP, B, C4, SIM).  Line length is 100.
`tests/fixtures/` is excluded — fixture files are sample Python code analysed by the
extractor, not project source to be linted.

### Type check — `mypy src`

Runs in strict mode against `src/cartogate`.  Two overrides are active:
- Untyped stubs for `psutil`, `networkx`, `jedi`, `tree_sitter_python` are suppressed
  (`ignore_missing_imports`).
- `cartogate.mcp.server` relaxes decorator and call strictness because the MCP SDK's
  low-level handler decorators are untyped.

### Tests — `pytest`

`pyproject.toml` configures:
```toml
[tool.pytest.ini_options]
testpaths    = ["tests"]
pythonpath   = ["."]
addopts      = "-ra -m 'not benchmark'"
```

Plain `pytest` therefore runs the **98-test suite** (unit + integration) and
automatically deselects the 2 benchmark tests.  The `-m benchmark` marker exists for
local worst-case SLO runs only — **never add `-m benchmark` to the CI command**.
Benchmarks are timing-sensitive and produce meaningless results on shared CI runners.

The integration suite includes a real-git test (`test_hooks.py::test_pre_commit_*`)
that creates a temporary git repository, stages files, and invokes the pre-commit hook.
GitHub Actions runners ship with git, so this works without extra setup.

### Security scan — `pip-audit` (blocking)

```yaml
- name: Upgrade build tooling
  run: python -m pip install --upgrade pip setuptools

- name: Security scan (pip-audit)
  run: pip-audit
```

`pip-audit` scans the installed environment for known CVEs via the PyPI advisory
database. It is **blocking** — a CVE fails the gate.

The first CI run showed `pip-audit` was non-blocking and flagged 5 CVEs, all in
`setuptools 65.5.0` — the stale version baked into the windows runner base image, not a
Cartogate dependency. The **Upgrade build tooling** step replaces that stale setuptools
before the scan, so the audit reflects our actual dependency tree, which is clean. If a
real dependency CVE appears, bump the offending package.

---

## Running the gate locally

Run these commands from the repository root in the same order as CI:

```bash
# 1. Install (once, or after any pyproject.toml change)
pip install -e ".[dev]"

# 2. Lint
ruff check .

# 3. Type check
mypy src

# 4. Tests (benchmarks auto-excluded by pyproject addopts)
pytest

# 5. Security scan (informational)
pip-audit
```

To run just the benchmark suite (local timing validation, never in CI):

```bash
pytest -m benchmark
```

---

## Local validation with `act`

`act` is the recommended tool for running GitHub Actions workflows locally before
pushing.  Install it from https://github.com/nektos/act and run:

```bash
act pull_request --matrix os:ubuntu-latest --matrix python-version:3.11
```

> **Note:** `act` was not available in the authoring environment (Windows without
> Docker Desktop).  The workflow was validated by:
> 1. Running all gate commands locally and confirming they pass.
> 2. Confirming the YAML has no tabs (GitHub Actions YAML must use spaces only).
> 3. Confirming all required top-level keys (`name`, `on`, `jobs`) are present.
> 4. Auditing action versions (`actions/checkout@v4`, `actions/setup-python@v5`).

---

## Triggers

| Event | Condition |
|-------|-----------|
| `push` | Branch `main` |
| `pull_request` | Target branch `main` |

The open PR `fix/duplicate-gate-method-false-positives → main` will receive a CI
check as soon as the workflow file lands on that branch.

---

## What is excluded from CI

- **Benchmarks** — deselected via pytest marker.  SLO timing is host-dependent.
- **`.dev/` directory** — agent-pipeline staging, reports, and trace logs.  Never
  packaged or deployed.  The `[tool.hatch.build.targets.wheel]` stanza in
  `pyproject.toml` limits the wheel to `src/cartogate` only.
- **Deployment** — there is no deployment target in v0.  The MCP server ships as a
  pip-installable package; publishing to PyPI is a future concern (see `FUTURE.md`).

---

## Changelog

| Date | Change |
|------|--------|
| 2026-06-24 | Initial CI workflow; ubuntu + windows matrix, Python 3.11 + 3.12, ruff/mypy/pytest/pip-audit gate |
| 2026-06-24 | Added `pythonpath = ["."]` to `[tool.pytest.ini_options]` so plain `pytest` resolves `from tests.conftest import ...` on both CI runners and locally |
| 2026-06-24 | Hardened `pip-audit` to blocking; added an "Upgrade build tooling" step to clear the stale-runner-setuptools CVEs (not Cartogate deps) so the scan reflects the real dependency tree |

> Note: the former `[extract]` and `[mcp]` extras are now empty back-compat aliases — extraction and the MCP SDK ship in the base install.
