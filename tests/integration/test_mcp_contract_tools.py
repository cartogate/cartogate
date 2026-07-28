"""PR C — the contract/ledger surface an AGENT can see (`contract_status`, `gate_history`).

v0.7.0 shipped verification contracts and the audit ledger, but exposed neither to the agent:
its first encounter with a locked contract was BEING REFUSED at session end, which is a
terrible way to learn the rules, and it had no way to notice it was repeating a blocked move.
Both tools are read-only and advisory — they never block, and they degrade (never raise) on a
missing root or corrupt state, because a gate surface that throws trains agents to avoid it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import init_git_repo as _init
from tests.conftest import write_contract as _write_contract

from cartogate.audit import ledger
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.store.memory import InMemoryStore
from cartogate.task_cli import main as task_main

_PY = "python"


def _tools(root: Path | None) -> CartogateTools:
    return CartogateTools(InMemoryStore(), root=root)


class TestContractStatus:
    """What the locked contract requires, and which parts are satisfied RIGHT NOW."""

    def test_no_root_degrades_with_a_note(self) -> None:
        result = _tools(None).contract_status()
        assert result["contract"] is None
        assert "note" in result

    def test_no_contract_is_not_an_error(self, tmp_path: Path) -> None:
        _init(tmp_path)
        result = _tools(tmp_path).contract_status()
        assert result["contract"] is None
        assert result["locked"] is False

    def test_reports_task_checks_and_lock_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        p = _write_contract(
            tmp_path,
            {"task": "ship the gate", "checks": [{"run": f'{_PY} -c "import sys; sys.exit(1)"'}]},
        )
        task_main(["declare", str(p), "--lock"])
        result = _tools(tmp_path).contract_status()
        assert result["contract"] == "ship the gate"
        assert result["locked"] is True
        assert result["satisfied"] is False
        assert result["checks"][0]["passed"] is False
        assert result["checks"][0]["run"]  # the agent can see WHAT it must make pass

    def test_satisfied_contract_reports_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        p = _write_contract(tmp_path, {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}]})
        task_main(["declare", str(p), "--lock"])
        result = _tools(tmp_path).contract_status()
        assert result["satisfied"] is True
        assert result["checks"][0]["passed"] is True

    def test_pending_attestations_are_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An attestation can only be satisfied by a human — naming it is the whole point."""
        _init(tmp_path)
        monkeypatch.chdir(tmp_path)
        p = _write_contract(
            tmp_path,
            {"task": "t", "checks": [{"run": f'{_PY} -c "print(1)"'}], "attest": ["ux_reviewed"]},
        )
        task_main(["declare", str(p), "--lock"])
        result = _tools(tmp_path).contract_status()
        assert result["satisfied"] is False
        assert result["pending_attestations"] == ["ux_reviewed"]

    def test_corrupt_state_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        from cartogate.contract import state

        _init(tmp_path)
        state.task_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        state.task_path(tmp_path).write_text("{broken", encoding="utf-8")
        result = _tools(tmp_path).contract_status()
        assert result["contract"] is None
        assert "note" in result


class TestGateHistory:
    """Recent gate decisions, so an agent can notice it is repeating a blocked move."""

    def test_no_root_degrades_with_a_note(self) -> None:
        result = _tools(None).gate_history()
        assert result["entries"] == []
        assert "note" in result

    def test_empty_ledger_is_not_an_error(self, tmp_path: Path) -> None:
        _init(tmp_path)
        assert _tools(tmp_path).gate_history()["entries"] == []

    def test_returns_newest_first_with_type_and_evidence(self, tmp_path: Path) -> None:
        _init(tmp_path)
        for i in range(3):
            ledger.append(
                tmp_path, entry_type="commit_block", tree=None, evidence={"signature": f"f{i}()"}
            )
        entries = _tools(tmp_path).gate_history()["entries"]
        assert [e["evidence"]["signature"] for e in entries] == ["f2()", "f1()", "f0()"]
        assert entries[0]["type"] == "commit_block"
        assert "ts" in entries[0]

    def test_limit_caps_the_window(self, tmp_path: Path) -> None:
        _init(tmp_path)
        for i in range(10):
            ledger.append(tmp_path, entry_type="commit_pass", tree=None, evidence={"n": i})
        assert len(_tools(tmp_path).gate_history(limit=4)["entries"]) == 4

    def test_absurd_limit_is_clamped_not_obeyed(self, tmp_path: Path) -> None:
        """A ledger can hold thousands of rows; an unbounded limit would flood the agent's
        context — the one resource a gate surface must not waste."""
        from cartogate.mcp.tools import GATE_HISTORY_MAX

        _init(tmp_path)
        for i in range(GATE_HISTORY_MAX + 5):
            ledger.append(tmp_path, entry_type="commit_pass", tree=None, evidence={"n": i})
        result = _tools(tmp_path).gate_history(limit=10_000)
        assert len(result["entries"]) == GATE_HISTORY_MAX
        assert result["total"] == GATE_HISTORY_MAX + 5  # the window is capped, the count is honest


class TestWiring:
    """A tool the agent can't discover or call doesn't exist."""

    @pytest.mark.parametrize("name", ["contract_status", "gate_history"])
    def test_tool_is_declared(self, name: str) -> None:
        spec = next((s for s in TOOL_SPECS if s["name"] == name), None)
        assert spec is not None, f"{name} missing from TOOL_SPECS"
        assert spec["description"].strip()
        assert spec["input_schema"]["type"] == "object"

    @pytest.mark.parametrize("name", ["contract_status", "gate_history"])
    def test_dispatch_routes_the_tool(self, name: str, tmp_path: Path) -> None:
        _init(tmp_path)
        assert isinstance(dispatch(_tools(tmp_path), name, {}), dict)


class TestDispatchArgumentCoercion:
    """`dispatch` converts numeric arguments before the tool method's own defenses run, so a
    junk scalar raised out of dispatch itself — past every fail-open guard inside the tool."""

    @pytest.mark.parametrize("limit", [None, "twenty", [], {}, 3.7])
    def test_gate_history_tolerates_a_junk_limit(self, limit: object, tmp_path: Path) -> None:
        _init(tmp_path)
        result = dispatch(_tools(tmp_path), "gate_history", {"limit": limit})
        assert isinstance(result["entries"], list)

    @pytest.mark.parametrize("tool", ["blast_radius", "suggest_tests", "impact_summary"])
    def test_depth_taking_tools_tolerate_a_junk_depth(self, tool: str, tmp_path: Path) -> None:
        """Same bug class, pre-existing: these coerce `depth` the same eager way."""
        _init(tmp_path)
        args = {"depth": None} | ({"symbol": "x"} if tool == "blast_radius" else {})
        assert isinstance(dispatch(_tools(tmp_path), tool, args), dict)
