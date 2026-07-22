"""Scope advisory — deviations are surfaced + ledgered, never blocked (spec §6.3, v1)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.conftest import git_cmd as _git
from tests.conftest import init_git_repo as _init

from cartogate.audit import ledger
from cartogate.precommit import main as precommit_main
from cartogate.task_cli import main as task_main

_PY = f'"{sys.executable}"'
# NOTE: shared git helpers from tests/conftest.py — never define local _git/_init copies.


def test_out_of_scope_edit_is_advisory_not_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("def g(y):\n    return y\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    contract = {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                "scope": {"files": ["src/*.py"]}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(contract), encoding="utf-8")
    assert task_main(["declare", str(p)]) == 0
    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER blocks (v1)
    err = capsys.readouterr().err
    assert "SCOPE ADVISORY" in err and "unrelated.py" in err
    types = [e["type"] for e in ledger.read(tmp_path)]
    assert "scope_deviation" in types


def test_in_scope_edit_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    contract = {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}],
                "scope": {"files": ["src/*.py", "c.json"]}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(contract), encoding="utf-8")
    task_main(["declare", str(p)])
    assert precommit_main([str(tmp_path)]) == 0
    assert "SCOPE ADVISORY" not in capsys.readouterr().err


def test_no_scope_declared_means_no_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]}),
                 encoding="utf-8")
    task_main(["declare", str(p)])
    assert precommit_main([str(tmp_path)]) == 0
    assert "SCOPE ADVISORY" not in capsys.readouterr().err


def test_scope_from_symbol_without_snapshot_errors_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path)
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]}),
                 encoding="utf-8")
    assert task_main(["declare", str(p), "--scope-from-symbol", "pkg.fn"]) == 1
    assert "snapshot" in capsys.readouterr().err  # actionable: run the daemon/index first


class _StubTools:
    """Duck-typed stand-in for CartogateTools — canned, VERIFIED result shapes."""

    def __init__(self, found: bool) -> None:
        self._found = found

    def blast_radius(self, symbol: str) -> dict:  # type: ignore[type-arg]
        affected = [{"unit": "repo/src/callers.py"}] if self._found else []
        return {"found": self._found, "affected": affected}

    def find_symbol(self, symbol: str) -> dict:  # type: ignore[type-arg]
        if not self._found:
            return {"found": False}
        # _node_full's location is a DICT {path, start_line, end_line} — not "path:line"
        # (review M1: the string-split fallback was dead code).
        return {"found": True,
                "location": {"path": "repo/src/own.py", "start_line": 1, "end_line": 2}}


def test_scope_expansion_uses_dict_location_and_strips_prefix() -> None:
    from cartogate.contract.schema import parse
    from cartogate.task_cli import _expand_scope_from_symbols

    c = parse({"task": "t", "checks": [{"run": "x -v"}]})
    out = _expand_scope_from_symbols(_StubTools(True), c, ["pkg.fn"])
    # The symbol's OWN file lands in scope independent of blast_radius edge defaults (M1),
    # and unit prefixes are stripped for both.
    assert "src/own.py" in out.scope_files
    assert "src/callers.py" in out.scope_files


def test_scope_expansion_refuses_unknown_symbol() -> None:
    import pytest as _pytest

    from cartogate.contract.schema import ContractError, parse
    from cartogate.task_cli import _expand_scope_from_symbols

    c = parse({"task": "t", "checks": [{"run": "x -v"}]})
    with _pytest.raises(ContractError, match="not found"):
        _expand_scope_from_symbols(_StubTools(False), c, ["pkg.nope"])  # refuse, don't guess (M2)
