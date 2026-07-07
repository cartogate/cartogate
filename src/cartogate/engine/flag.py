"""FLAG mode — change-reactive, advisory drift detection (spec §1, §10 Phase 1).

FLAG is a *detector, not a generator*: it never blocks. Given the symbols a change touched, it
surfaces the **tests that exercise them** so they can be reviewed/re-run. (Doc-drift — docs that
reference a changed symbol — is the next FLAG increment; see FUTURE.)

Test-drift reuses the resolved call/reference graph: a "test" referencing a symbol is a caller
of that symbol (over ``calls``/``references`` edges) whose owning unit is a test file. Nothing
is materialized — the relationship is computed from edges that already exist. It rests on
``EXTRACTED`` edges (deterministic), matching the gate's evidence standard even though the
output is advisory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from cartogate.engine.diff import parse_unified_diff
from cartogate.engine.traversal import EXERCISE_EDGE_TYPES
from cartogate.schema.enums import Confidence, EdgeType, NodeKind
from cartogate.schema.nodes import Node
from cartogate.store.base import StoreInterface

#: A test *exercises* a symbol via one of these edges — a call, a reference, or a subclass/
#: implementation (a test double). Excludes bare imports (see EXERCISE_EDGE_TYPES).
_REFERENCE_EDGES = EXERCISE_EDGE_TYPES
#: A doc references a symbol via this edge.
_DOCUMENTS_EDGES = (EdgeType.DOCUMENTS,)
#: Directory names that mark a test tree (Python ``tests``/``test``; JS/TS ``__tests__``).
_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
#: Filename infixes that mark a JS/TS test file (``foo.test.ts``, ``foo.spec.tsx``).
_TS_TEST_INFIXES = (".test.", ".spec.")
#: JUnit filename conventions (``FooTest.java``, ``FooTests.java``, ``TestFoo.java``).
_JAVA_TEST_SUFFIXES = ("Test.java", "Tests.java", "IT.java")
_JAVA_TEST_PREFIX = "Test"
#: Go test files end in ``_test.go``.
_GO_TEST_SUFFIX = "_test.go"
#: C# test files (xUnit/NUnit/MSTest) conventionally end in ``Test.cs``/``Tests.cs``.
_CSHARP_TEST_SUFFIXES = ("Test.cs", "Tests.cs")
#: C/C++ test files (GoogleTest/Catch) conventionally end in ``_test.{c,cc,cpp,cxx}``.
_C_FAMILY_TEST_SUFFIXES = ("_test.c", "_test.cc", "_test.cpp", "_test.cxx")
#: Kotlin test files (JUnit) conventionally end in ``Test.kt``/``Tests.kt``.
_KOTLIN_TEST_SUFFIXES = ("Test.kt", "Tests.kt")
#: Swift (XCTest) test files conventionally end in ``Tests.swift``/``Test.swift``.
_SWIFT_TEST_SUFFIXES = ("Tests.swift", "Test.swift")


def is_test_unit(unit: str) -> bool:
    """Whether a unit (POSIX file path) is a test file.

    True if it lives under a ``tests``/``test``/``__tests__`` directory (Java's ``src/test/…``
    matches via the ``test`` segment), or its filename matches the common conventions: Python
    (``test_*.py``, ``*_test.py``, ``conftest.py``), JS/TS (``*.test.ts``/``.tsx``,
    ``*.spec.ts``/``.tsx``), or Java/JUnit (``*Test.java``, ``*Tests.java``, ``*IT.java``,
    ``Test*.java``). A non-source file under a test dir classifies as a test too — benign, since
    FLAG is advisory.
    """
    path = PurePosixPath(unit)
    if _TEST_DIRS.intersection(path.parts[:-1]):  # a test directory anywhere in the path
        return True
    name = path.name
    # Filename conventions only — a bare `test.py` (no `test_` prefix) is deliberately NOT a test.
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name.endswith(_JAVA_TEST_SUFFIXES) or (name.startswith(_JAVA_TEST_PREFIX)
                                              and name.endswith(".java")):
        return True
    if (name.endswith(_GO_TEST_SUFFIX) or name.endswith(_CSHARP_TEST_SUFFIXES)
            or name.endswith(_C_FAMILY_TEST_SUFFIXES) or name.endswith(_KOTLIN_TEST_SUFFIXES)
            or name.endswith(_SWIFT_TEST_SUFFIXES)):
        return True
    return any(infix in name for infix in _TS_TEST_INFIXES)


@dataclass(frozen=True, slots=True)
class _FlaggedTest:
    qualified_name: str
    unit: str
    exercises: tuple[str, ...]  # the changed symbols this test references


@dataclass(frozen=True, slots=True)
class FlagReport:
    """The advisory result of a FLAG query: tests exercising the changed symbols."""

    changed: tuple[str, ...]
    tests: tuple[_FlaggedTest, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": list(self.changed),
            "tests": [
                {"qualified_name": t.qualified_name, "unit": t.unit, "exercises": list(t.exercises)}
                for t in self.tests
            ],
            "count": len(self.tests),
        }


@dataclass(frozen=True, slots=True)
class _FlaggedDoc:
    path: str  # the doc_section's qualified_name (its file path)
    unit: str
    mentions: tuple[str, ...]  # the changed symbols this doc references


@dataclass(frozen=True, slots=True)
class DocReport:
    """The advisory result of a doc-drift query: docs referencing the changed symbols."""

    changed: tuple[str, ...]
    docs: tuple[_FlaggedDoc, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": list(self.changed),
            "docs": [
                {"path": d.path, "unit": d.unit, "mentions": list(d.mentions)} for d in self.docs
            ],
            "count": len(self.docs),
        }


class FlagEngine:
    """Computes drift: which tests/docs reference a set of changed symbols (advisory)."""

    def __init__(self, store: StoreInterface) -> None:
        self._store = store

    def tests_for_symbols(self, qualified_names: list[str], *, depth: int = 1) -> FlagReport:
        """Flag the tests that reference any of ``qualified_names``."""
        changed: list[str] = []
        # test qualified_name -> the changed symbols it exercises (sorted, deduped)
        exercised: dict[tuple[str, str], set[str]] = {}
        for qname in qualified_names:
            node = self._store.get_symbol(qname)
            if node is None:
                continue
            changed.append(qname)
            for test in self._tests_referencing(node, depth):
                exercised.setdefault((test.qualified_name, test.unit), set()).add(qname)
        return self._build_report(changed, exercised)

    def tests_for_diff(self, diff_text: str, *, depth: int = 1) -> FlagReport:
        """Flag the tests exercising the symbols a unified diff changed."""
        return self.tests_for_symbols(self._changed_symbol_qnames(diff_text), depth=depth)

    def docs_for_symbols(self, qualified_names: list[str], *, depth: int = 1) -> DocReport:
        """Flag the docs that explicitly reference any of ``qualified_names``."""
        changed: list[str] = []
        referenced: dict[tuple[str, str], set[str]] = {}
        for qname in qualified_names:
            node = self._store.get_symbol(qname)
            if node is None:
                continue
            changed.append(qname)
            for doc in self._docs_referencing(node, depth):
                referenced.setdefault((doc.qualified_name, doc.unit), set()).add(qname)
        docs = tuple(
            _FlaggedDoc(path=path, unit=unit, mentions=tuple(sorted(symbols)))
            for (path, unit), symbols in sorted(referenced.items())
        )
        return DocReport(changed=tuple(sorted(set(changed))), docs=docs)

    def docs_for_diff(self, diff_text: str, *, depth: int = 1) -> DocReport:
        """Flag the docs referencing the symbols a unified diff changed."""
        return self.docs_for_symbols(self._changed_symbol_qnames(diff_text), depth=depth)

    # ------------------------------------------------------------------ #

    def _changed_symbol_qnames(self, diff_text: str) -> list[str]:
        """Map a unified diff to the qualified names of the changed SYMBOL nodes."""
        changed_ids = self._store.changed_set(parse_unified_diff(diff_text))
        return sorted(
            {
                node.qualified_name
                for node_id in changed_ids
                if (node := self._store.get_node(node_id)) is not None
                and node.kind is NodeKind.SYMBOL
            }
        )

    def _tests_referencing(self, node: Node, depth: int) -> list[Node]:
        callers = self._store.callers_of(
            node.id, depth=depth, edge_types=_REFERENCE_EDGES, confidence=(Confidence.EXTRACTED,)
        )
        return [c for c in callers if is_test_unit(c.unit)]

    def _docs_referencing(self, node: Node, depth: int) -> list[Node]:
        # Callers over DOCUMENTS edges are doc_section nodes (no unit filter needed).
        return self._store.callers_of(
            node.id, depth=depth, edge_types=_DOCUMENTS_EDGES, confidence=(Confidence.EXTRACTED,)
        )

    @staticmethod
    def _build_report(
        changed: list[str], exercised: dict[tuple[str, str], set[str]]
    ) -> FlagReport:
        tests = tuple(
            _FlaggedTest(qualified_name=qn, unit=unit, exercises=tuple(sorted(symbols)))
            for (qn, unit), symbols in sorted(exercised.items())
        )
        return FlagReport(changed=tuple(sorted(set(changed))), tests=tests)
