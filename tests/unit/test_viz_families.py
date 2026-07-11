"""Tests for the deterministic unit -> family classifier (viz families view)."""

from __future__ import annotations

from cartogate.viz.families import FAMILIES, classify, family_of


def test_externals_unit_maps_to_external_family() -> None:
    # "<externals>" is the pipeline's synthetic unit for external packages — exact match,
    # never path-parsed (it contains <> and has no repo prefix).
    assert family_of("<externals>") == "external"


def test_repo_prefixed_paths_classify_by_second_segment() -> None:
    # Real units are repo-name-prefixed (index base = root.parent) — segment 0 is skipped.
    assert family_of("repo/src/pkg/store.py") == "core"
    assert family_of("repo/tests/unit/x.py") == "tests"
    assert family_of("repo/docs/guide.md") == "docs"
    assert family_of("repo/.github/workflows/ci.yml") == "ci"
    assert family_of("repo/hooks/pre_commit.py") == "ci"
    assert family_of("repo/evaluation/run.py") == "examples"
    assert family_of("repo/benchmarks/bench.py") == "examples"
    assert family_of("repo/scripts/gen.py") == "scripts"
    assert family_of("repo/tools/fix.py") == "scripts"


def test_filename_patterns_beat_directories() -> None:
    assert family_of("repo/src/pkg/test_store.py") == "tests"
    assert family_of("repo/src/pkg/conftest.py") == "tests"
    assert family_of("repo/src/pkg/store_test.go") == "tests"
    assert family_of("repo/setup.py") == "ci"
    assert family_of("repo/noxfile.py") == "ci"


def test_single_segment_units_do_not_crash_and_default_core() -> None:
    # Fixture-style units have no repo prefix; filename rules still apply, dirs are empty.
    assert family_of("m.py") == "core"
    assert family_of("test_m.py") == "tests"


def test_directory_precedence_tests_beats_scripts_and_ci() -> None:
    assert family_of("repo/tools/tests/x.py") == "tests"
    assert family_of("repo/.github/tests/x.py") == "tests"


def test_a_repo_literally_named_tests_is_not_misclassified() -> None:
    # Segment 0 is ALWAYS the repo prefix — a repo named "tests" must not drag
    # everything into the tests family.
    assert family_of("tests/pkg/store.py") == "core"


def test_totality_and_determinism() -> None:
    units = ["<externals>", "m.py", "repo/a/b/c.py", "repo/docs/x.md", "weird//", ""]
    first = classify(units)
    assert first == classify(list(reversed(units)))
    assert set(first.values()) <= set(FAMILIES)
    assert set(first) == set(units)
