"""Real-repo, independent-oracle study (Phase 2 of the value study).

Replaces the self-authored fixture as the *headline* evidence: it runs Cartogate against a
real third-party repo (the pinned click snapshot) and scores it with ground truth from tools
**independent of Cartogate** — pyright for references (V3), coverage.py for test selection
(V7) — plus objective duplicate injection (V4). Opt-in (needs Node/pyright and runs a foreign
test suite); not in CI. See ``evaluation/realstudy/README.md``.
"""
