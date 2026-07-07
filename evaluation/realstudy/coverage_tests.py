"""coverage.py test-selection oracle (V7) — runtime ground truth, independent of Cartogate.

Runs the corpus's own test suite under ``pytest --cov --cov-context=test`` so coverage records,
per source line, which *test* executed it. The tests that truly exercise a symbol = the tests
whose dynamic context covered any line of that symbol's definition. That is an objective runtime
fact (not a static guess and not Cartogate's own output), so it fairly grades ``suggest_tests``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import coverage

#: A pytest-cov dynamic context looks like ``tests/test_x.py::test_y[param]|run``.
_CONTEXT_RE = re.compile(r"^(?P<id>.*?::[^|\[]+)")


def normalize_context(ctx: str) -> str:
    """Reduce a coverage context to a stable ``file::testfunc`` id (drop params + phase)."""
    match = _CONTEXT_RE.match(ctx)
    return match.group("id") if match else ""


def cartogate_test_id(qualified_name: str, unit: str) -> str:
    """Map a Cartogate test symbol (qname + unit) to coverage.py's ``file::test`` context id.

    pytest names a plain test ``file::test_x`` and a ``unittest``/class method
    ``file::TestClass::test_x``. So: a **Capitalized** first component is a TestCase class →
    keep ``Class::method`` (matching coverage); otherwise it's a function-style test → keep just
    the function and drop any nested closures (e.g. a click command defined inside a test).
    """
    module = unit.removesuffix(".py").replace("/", ".")
    if qualified_name.startswith(module + "."):
        rest = qualified_name[len(module) + 1:]
    else:
        rest = qualified_name.rsplit(".", 1)[-1]
    parts = rest.split(".")
    # A unittest class method -> ``Class::method`` (matches coverage); else a function-style test,
    # keeping just the function (dropping any nested closures).
    is_class_method = len(parts) >= 2 and parts[0][:1].isupper()
    testfunc = f"{parts[0]}::{parts[1]}" if is_class_method else parts[0]
    return f"{unit}::{testfunc}"


class CoverageOracle:
    """Runs the suite once and answers ``tests_covering(file, start, end)``."""

    def __init__(self, work: Path, package: str, *, timeout: float = 900.0) -> None:
        self.work = work
        self.package = package
        self._timeout = timeout
        self._by_file: dict[Path, dict[int, list[str]]] = {}
        self._all_tests: set[str] = set()
        self.summary: str = ""

    def run(self) -> CoverageOracle:
        covfile = self.work / ".coverage.realstudy"
        env = {**os.environ, "PYTHONPATH": str(self.work), "COVERAGE_FILE": str(covfile)}
        proc = subprocess.run(
            ["python", "-m", "pytest", "tests", f"--cov={self.package}", "--cov-context=test",
             "--cov-report=", "-q", "-p", "no:cacheprovider"],
            cwd=self.work, env=env, capture_output=True, text=True, timeout=self._timeout,
        )
        self.summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""

        data = coverage.CoverageData(basename=str(covfile))
        data.read()
        for measured in data.measured_files():
            by_line = data.contexts_by_lineno(measured)
            self._by_file[Path(measured).resolve()] = by_line
            for contexts in by_line.values():
                for ctx in contexts:
                    tid = normalize_context(ctx)
                    if tid:
                        self._all_tests.add(tid)
        return self

    @property
    def total_tests(self) -> int:
        return len(self._all_tests)

    def tests_covering(self, abs_file: Path, start_line: int, end_line: int) -> set[str]:
        by_line = self._by_file.get(abs_file.resolve(), {})
        out: set[str] = set()
        for line in range(start_line, end_line + 1):
            for ctx in by_line.get(line, []):
                tid = normalize_context(ctx)
                if tid:
                    out.add(tid)
        return out
