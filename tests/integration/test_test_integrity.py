"""Test-integrity advisory (STRATEGY.md Phase 2) — the reward-hacking counter.

METR measured frontier models reward-hacking 30% of hard tasks; the common shape is weakening
the verification signal: deleting/loosening assertions, adding skip/xfail, hardcoding expected
values. The deterministic, extracted-facts subset: when a commit touches BOTH source and test
files (the discriminating "fix passes because the test got weaker" pattern), compare each staged
test file against HEAD and report assertion-count drops, added skip markers, and deleted tests.
A pure test-refactor commit (no source touched) stays silent — precision first, advisory-only,
never affects the exit code.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cartogate.precommit import main as precommit_main

SRC = "def fix(x):\n    return x + 1\n"
TESTS_STRONG = (
    "import pytest\n\n"
    "def test_a():\n    assert fix(1) == 2\n    assert fix(2) == 3\n\n"
    "def test_b():\n    assert fix(0) == 1\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"],
        },
    )


def _seed(repo: Path) -> None:
    _git(repo, "init", "-q")
    (repo / "app.py").write_text(SRC, encoding="utf-8")
    (repo / "test_app.py").write_text(TESTS_STRONG, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", "--no-verify")


def _stage_src_change(repo: Path) -> None:
    (repo / "app.py").write_text("def fix(x):\n    return x + 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")


def test_weakened_assertions_alongside_a_src_change_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    (tmp_path / "test_app.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    pass\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER affects the exit
    err = capsys.readouterr().err
    assert "TEST-INTEGRITY" in err
    assert "test_app.py" in err and "assertions 3 -> 1" in err
    assert "ACTION:" in err


def test_added_skip_marker_alongside_a_src_change_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    (tmp_path / "test_app.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_a():\n    assert fix(1) == 2\n    assert fix(2) == 3\n\n"
        "def test_b():\n    assert fix(0) == 1\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "TEST-INTEGRITY" in err and "skip/xfail markers 0 -> 1" in err


def test_deleted_test_function_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    (tmp_path / "test_app.py").write_text(
        "def test_a():\n    assert fix(1) == 2\n    assert fix(2) == 3\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "test functions 2 -> 1" in capsys.readouterr().err


def test_deleted_test_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    _git(tmp_path, "rm", "-q", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "TEST-INTEGRITY" in err and "deleted" in err


def test_pure_test_refactor_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No source touched -> legitimate test work, not the reward-hack pattern."""
    _seed(tmp_path)
    (tmp_path / "test_app.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "TEST-INTEGRITY" not in capsys.readouterr().err


def test_strengthened_tests_are_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    (tmp_path / "test_app.py").write_text(
        TESTS_STRONG + "\ndef test_c():\n    assert fix(5) == 7\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "test_app.py")

    assert precommit_main([str(tmp_path)]) == 0
    assert "TEST-INTEGRITY" not in capsys.readouterr().err


def test_fixture_trees_never_fire(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Files under a fixtures dir are sample code to ANALYZE, not this repo's tests."""
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    fx = tmp_path / "tests" / "fixtures" / "proj"
    fx.mkdir(parents=True)
    (fx / "test_sample.py").write_text(
        "def test_s():" + chr(10) + "    pass" + chr(10), encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    assert precommit_main([str(tmp_path)]) == 0
    assert "test_sample.py" not in capsys.readouterr().err


def test_unittest_style_assertions_are_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path)
    unittest_tests = (
        "import unittest" + chr(10) + chr(10)
        + "class TestApp(unittest.TestCase):" + chr(10)
        + "    def test_a(self):" + chr(10)
        + "        self.assertEqual(fix(1), 2)" + chr(10)
        + "        self.assertTrue(fix(0))" + chr(10)
    )
    (tmp_path / "test_app.py").write_text(unittest_tests, encoding="utf-8")
    _git(tmp_path, "add", "test_app.py")
    _git(tmp_path, "commit", "-q", "-m", "unittest", "--no-verify")
    _stage_src_change(tmp_path)
    weakened = unittest_tests.replace("        self.assertTrue(fix(0))" + chr(10), "")
    (tmp_path / "test_app.py").write_text(weakened, encoding="utf-8")
    _git(tmp_path, "add", "test_app.py")
    assert precommit_main([str(tmp_path)]) == 0
    assert "assertions 2 -> 1" in capsys.readouterr().err


def test_parametrize_conversion_is_annotated_not_accused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Consolidating asserts into @pytest.mark.parametrize is idiomatic — the drop is
    reported WITH the consolidation context, not as bare weakening (review MED-1)."""
    _seed(tmp_path)
    _stage_src_change(tmp_path)
    consolidated = (
        "import pytest" + chr(10) + chr(10)
        + "@pytest.mark.parametrize(chr(39)x,y" + chr(39) + ", [(1, 2), (2, 3), (0, 1)])" + chr(10)
        + "def test_fix(x, y):" + chr(10)
        + "    assert fix(x) == y" + chr(10)
    )
    consolidated = consolidated.replace("chr(39)x,y", chr(39) + "x,y")
    (tmp_path / "test_app.py").write_text(consolidated, encoding="utf-8")
    _git(tmp_path, "add", "test_app.py")
    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "assertions 3 -> 1" in err
    assert "may be consolidation" in err  # annotated, not accused


def test_cross_file_assertion_move_gets_the_net_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Moving asserts between test files is not a net loss — the note says so
    alongside the per-file drop (review MED-2)."""
    _seed(tmp_path)
    (tmp_path / "test_extra.py").write_text(
        "def test_x():" + chr(10) + "    assert fix(9) == 10" + chr(10), encoding="utf-8"
    )
    _git(tmp_path, "add", "test_extra.py")
    _git(tmp_path, "commit", "-q", "-m", "extra", "--no-verify")
    _stage_src_change(tmp_path)
    # move two asserts from test_app.py into test_extra.py
    (tmp_path / "test_app.py").write_text(
        "def test_b():" + chr(10) + "    assert fix(0) == 1" + chr(10), encoding="utf-8"
    )
    (tmp_path / "test_extra.py").write_text(
        "def test_x():" + chr(10) + "    assert fix(9) == 10" + chr(10)
        + "    assert fix(1) == 2" + chr(10) + "    assert fix(2) == 3" + chr(10),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")
    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "test_app.py: assertions 3 -> 1" in err
    assert "no net loss" in err


def test_non_git_directory_is_harmless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text(SRC, encoding="utf-8")
    assert precommit_main([str(tmp_path)]) == 0
    assert "TEST-INTEGRITY" not in capsys.readouterr().err
