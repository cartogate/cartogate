"""Cartogate value study — empirical evidence for what developers gain.

Layer 1 (this package) is the *deterministic*, in-process measurement layer: it runs
Cartogate against a labeled corpus and a realistic "what you'd do without it" baseline,
computes a metric per hypothesis (V2–V10), asserts a regression threshold, and records
the numbers to ``evaluation/value_results.json``. It needs no network and no API key, so it is
reproducible by anyone and runs in CI.

Layer 2 (the top-level ``evaluation/`` package) is the *live agent A/B* layer for V1
(token usage) — that one needs the Claude API and is run manually.

See ``docs/VALUE_STUDY.md`` (generated from the recorded results) for the full report.
"""
