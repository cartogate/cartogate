"""``cartogate slice`` CLI + the `slice` MCP tool — end-to-end program slicing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate import slice_cli
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.store import InMemoryStore

_SRC = (
    "def f():\n"  # 1
    "    a = 1\n"  # 2
    "    b = 2\n"  # 3  (irrelevant to c)
    "    c = a + 3\n"  # 4
    "    return c\n"  # 5
)


def test_slice_cli_backward(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "m.py"
    f.write_text(_SRC, encoding="utf-8")
    rc = slice_cli.main([f"{f}:5", "--json"])  # slice from `return c`
    out = capsys.readouterr().out
    assert rc == 0
    lines = json.loads(out)["lines"]
    assert 2 in lines and 4 in lines and 5 in lines  # a=1, c=a+3, return c
    assert 3 not in lines  # b=2 is irrelevant


def test_slice_cli_forward(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "m.py"
    f.write_text(_SRC, encoding="utf-8")
    rc = slice_cli.main([f"{f}:2", "--forward", "--json"])  # what `a = 1` affects
    out = capsys.readouterr().out
    assert rc == 0
    lines = json.loads(out)["lines"]
    assert 2 in lines  # the seed line is always in its own slice
    assert 4 in lines and 5 in lines and 3 not in lines


def test_slice_cli_non_python_is_graceful(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello\n", encoding="utf-8")
    rc = slice_cli.main([f"{f}:1"])
    assert rc == 0 and "unsupported language" in capsys.readouterr().out  # advisory, not an error


def test_slice_cli_malformed_target(capsys: pytest.CaptureFixture[str]) -> None:
    rc = slice_cli.main(["no_line_here.py"])
    assert rc == 1 and "expected <file>:<line>" in capsys.readouterr().err


def test_slice_cli_unreadable_file_is_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = slice_cli.main([f"{tmp_path / 'missing.py'}:1"])
    assert rc == 1 and "cannot read" in capsys.readouterr().err


def test_slice_cli_line_outside_function_is_graceful(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "m.py"
    f.write_text("TOP = 1\n\ndef g():\n    return TOP\n", encoding="utf-8")
    rc = slice_cli.main([f"{f}:1"])  # line 1 is module-level, not inside a function
    assert rc == 0 and "no statement inside a function" in capsys.readouterr().out


def test_slice_mcp_tool_dispatch() -> None:
    out = dispatch(CartogateTools(InMemoryStore()), "slice", {"source": _SRC, "line": 5})
    assert out["found"] is True
    assert 2 in out["lines"] and 4 in out["lines"] and 3 not in out["lines"]


def test_slice_mcp_tool_found_false() -> None:
    # A line not inside any function -> found=False with a reason (matches the localize convention).
    out = dispatch(CartogateTools(InMemoryStore()), "slice", {"source": "X = 1\n", "line": 1})
    assert out["found"] is False and "reason" in out


_TS = (
    "function f() {\n"  # 1
    "  const a = 1;\n"  # 2
    "  const b = 2;\n"  # 3  (irrelevant to c)
    "  const c = a + 3;\n"  # 4
    "  return c;\n"  # 5
    "}\n"
)


def test_slice_cli_typescript_backward(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "m.ts"
    f.write_text(_TS, encoding="utf-8")
    rc = slice_cli.main([f"{f}:5", "--json"])  # slice from `return c`
    out = capsys.readouterr().out
    assert rc == 0
    lines = json.loads(out)["lines"]
    assert 2 in lines and 4 in lines and 5 in lines and 3 not in lines


def test_slice_mcp_tool_typescript() -> None:
    out = dispatch(
        CartogateTools(InMemoryStore()),
        "slice",
        {"source": _TS, "line": 5, "language": "typescript"},
    )
    assert out["found"] is True
    assert 2 in out["lines"] and 4 in out["lines"] and 3 not in out["lines"]
