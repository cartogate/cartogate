"""Deterministic, total unit -> family classifier for the viz (path heuristics, no config).

A *family* is a coarse role a file plays in the repository — the unit of aggregation for the
viz's landing view and of the hide/reveal controls (core code first; tests/docs/ci opt-in).
Classification is pure path heuristics so it just works on any repo:

- ``"<externals>"`` (the pipeline's synthetic unit for external packages) is matched exactly,
  before any path parsing.
- Real units are repo-name-prefixed (``"repo/src/pkg/x.py"`` — the index base is the repo's
  parent), so segment 0 is ALWAYS treated as the repo prefix and skipped. A repo literally
  named ``tests`` therefore classifies by its inner segments, correctly.
- Filename patterns beat directory names (``test_*.py`` under ``src/`` is still a test).
- Unprefixed single-segment units (test fixtures) never crash: directory rules see an empty
  list and filename rules still apply; the default is ``core``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Every family the classifier can produce, in display/precedence-index order.
FAMILIES: tuple[str, ...] = ("core", "external", "tests", "docs", "examples", "scripts", "ci")

#: The pipeline's synthetic unit holding EXTERNAL_PACKAGE nodes (pipeline.EXTERNALS_UNIT).
_EXTERNALS_UNIT = "<externals>"
#: Test files by name, wherever they live.
_TEST_FILE = re.compile(r"^(test_[^/]*|[^/]*_test\.[^/.]+|conftest\.py)$")
#: Root-level build/packaging files that belong with CI rather than shipped code.
_CI_FILES = frozenset({"setup.py", "noxfile.py"})
#: Directory-name rules in precedence order (first match wins).
_DIR_FAMILY: tuple[tuple[str, frozenset[str]], ...] = (
    ("tests", frozenset({"tests", "test", "testing"})),
    ("ci", frozenset({"ci", "hooks"})),  # plus any dot-directory (checked at this slot)
    ("docs", frozenset({"docs", "doc"})),
    (
        "examples",
        frozenset({
            "examples", "example", "samples", "sample", "demos", "demo",
            "evaluation", "benchmarks", "benchmark",
        }),
    ),
    ("scripts", frozenset({"scripts", "script", "tools", "tool", "bin"})),
)


def family_of(unit: str) -> str:
    """The family for one unit path. Total (never raises) and deterministic."""
    if unit == _EXTERNALS_UNIT:
        return "external"
    segments = unit.split("/")
    filename = segments[-1].lower()
    # Segment 0 is the repo prefix; only the segments BETWEEN it and the filename are dirs.
    dirs = [d.lower() for d in segments[1:-1]]
    if _TEST_FILE.match(filename):
        return "tests"
    if filename in _CI_FILES:
        return "ci"
    for family, names in _DIR_FAMILY:
        if any(d in names for d in dirs):
            return family
        if family == "ci" and any(d.startswith(".") for d in dirs):
            return "ci"
    return "core"


def classify(units: Iterable[str]) -> dict[str, str]:
    """Family per unit, keyed in sorted-unit order (deterministic iteration for callers)."""
    return {u: family_of(u) for u in sorted(set(units))}
