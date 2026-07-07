"""Layer 2 of the value study — the live agent A/B harness (V1: token usage).

This package runs a real Claude agent over a corpus twice per task: once **with** the
Cartogate tools available and once **without** (generic read/list/grep only), and records
the token cost of each arm. It needs the Anthropic SDK and ``ANTHROPIC_API_KEY``, so it is
opt-in and never runs in CI — its results are committed to ``evaluation/value_results.json`` for
retraceability. See ``evaluation/README.md``.
"""
