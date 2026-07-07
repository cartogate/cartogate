"""``cartogate cfg`` CLI — repo-wide statement-level unreachable-code scan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate import cfg_cli


def test_cfg_cli_finds_unreachable_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.py").write_text(
        "def f():\n    return 1\n    dead = 2\n", encoding="utf-8"  # `dead = 2` is unreachable
    )
    (tmp_path / "b.py").write_text("def g():\n    return 3\n", encoding="utf-8")  # all live
    rc = cfg_cli.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["count"] == 1
    hit = data["unreachable"][0]
    assert hit["path"] == "a.py" and hit["line"] == 3 and "dead = 2" in hit["code"]


def test_cfg_cli_clean_tree_reports_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.py").write_text(
        "def f(x):\n    if x:\n        return 1\n    return 2\n", encoding="utf-8"
    )
    rc = cfg_cli.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No unreachable statements found" in out


def test_cfg_cli_scans_typescript(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The scan is cross-language: a .ts file's unreachable statement is found too (F-08).
    (tmp_path / "m.ts").write_text(
        "function f(): number {\n  return 1;\n  const dead = 2;\n}\n", encoding="utf-8"
    )
    rc = cfg_cli.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["count"] == 1
    assert data["unreachable"][0]["path"] == "m.ts" and data["unreachable"][0]["line"] == 3


def test_cfg_cli_scans_go(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "m.go").write_text(
        "package m\nfunc f() int {\n\treturn 1\n\tdead := 2\n\treturn dead\n}\n", encoding="utf-8"
    )
    rc = cfg_cli.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert {h["line"] for h in data["unreachable"] if h["path"] == "m.go"} == {4, 5}


def test_cfg_cli_scans_java(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "M.java").write_text(
        "class M {\n  int f() {\n    return 1;\n    int dead = 2;\n  }\n}\n", encoding="utf-8"
    )
    rc = cfg_cli.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert {h["line"] for h in data["unreachable"] if h["path"] == "M.java"} == {4}


def test_cfg_cli_scans_c(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "m.c").write_text(
        "int f() {\n  return 1;\n  int dead = 2;\n}\n", encoding="utf-8"
    )
    rc = cfg_cli.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert {h["line"] for h in data["unreachable"] if h["path"] == "m.c"} == {3}
