"""`cartogate stats` — repo insight + a persistent tally of prevented duplicates."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cartogate import doctor
from cartogate.stats import read_blocks, record_block, run, summarize


def test_record_and_read_blocks_round_trip(tmp_path: Path) -> None:
    record_block(tmp_path, kind="commit", signature="foo(x)", language="python", existing="a.foo")
    record_block(tmp_path, kind="commit", signature="bar(y)", language="go", existing="b.Bar")
    blocks = read_blocks(tmp_path)
    assert [b["signature"] for b in blocks] == ["foo(x)", "bar(y)"]
    assert blocks[0]["kind"] == "commit" and "ts" in blocks[0]


def test_read_blocks_empty_when_none(tmp_path: Path) -> None:
    assert read_blocks(tmp_path) == []


def test_summarize_reports_graph_and_blocks(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    record_block(tmp_path, kind="commit", signature="dup()", language="python", existing="x.dup")

    s = summarize(tmp_path)
    assert s["symbols"] >= 1
    assert s["languages"].get("python", 0) >= 1
    assert s["blocks_total"] == 1
    assert s["last_block"]["signature"] == "dup()"


def test_precommit_records_a_prevented_duplicate(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def authenticate(n):\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def authenticate(n):\n    return 2\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "cartogate.precommit", str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 1
    blocks = read_blocks(tmp_path)
    assert len(blocks) == 1
    assert blocks[0]["signature"] == "authenticate(n)"
    assert blocks[0]["kind"] == "commit"


def test_stats_run_prints_prevented(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    record_block(tmp_path, kind="commit", signature="dup()", language="python", existing="x.dup")
    assert run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "duplicate-introducing commit(s) refused" in out


def test_doctor_surfaces_prevented_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "m.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    record_block(tmp_path, kind="commit", signature="dup()", language="python", existing="x.dup")
    doctor.run(tmp_path)
    assert "prevented 1 duplicate-introducing commit" in capsys.readouterr().out
