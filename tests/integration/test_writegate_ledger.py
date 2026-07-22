"""A write-time BLOCK records a ``write_block`` entry in the tamper-evident audit ledger."""

from __future__ import annotations

from pathlib import Path

from cartogate import writegate
from cartogate.audit import ledger


def test_write_block_is_recorded_to_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def authenticate(name):\n    return 1\n", encoding="utf-8")
    payload: dict[str, object] = {
        "tool_input": {
            "file_path": str(tmp_path / "n.py"),
            "content": "def authenticate(name):\n    return 2\n",
        }
    }
    env = {"CARTOGATE_REPO": str(tmp_path), "CARTOGATE_REPO_ID": "t"}
    code = writegate.run(payload, env=env, cwd=tmp_path)

    assert code == writegate.EXIT_BLOCK
    blocks = [e for e in ledger.read(tmp_path) if e["type"] == "write_block"]
    assert len(blocks) == 1
    assert blocks[0]["tree"] is None  # a write-time block is not a commit — never git-anchored
    assert blocks[0]["evidence"]["existing"].endswith("authenticate")
    assert blocks[0]["evidence"]["language"] == "python"


def test_passing_write_records_nothing(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def alpha(x):\n    return x\n", encoding="utf-8")
    payload: dict[str, object] = {
        "tool_input": {
            "file_path": str(tmp_path / "n.py"),
            "content": "def totally_novel_symbol_qqq(z):\n    return z\n",
        }
    }
    env = {"CARTOGATE_REPO": str(tmp_path), "CARTOGATE_REPO_ID": "t"}
    assert writegate.run(payload, env=env, cwd=tmp_path) == writegate.EXIT_OK
    assert ledger.read(tmp_path) == []
