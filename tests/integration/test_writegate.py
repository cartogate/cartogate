"""F-13 — the write-time gate as a FIRST-CLASS packaged surface (`cartogate.writegate`).

The research says hooks are the only deterministic enforcement
layer, so the adapter graduates from a repo-local script to an installed module + console
script that `cartogate init --agent <tool>` can wire into any repo: one command
(`cartogate-write-gate`), auto-detecting Claude's ``tool_input`` and Windsurf's ``tool_info``
payload shapes, exit 2 + the BLOCKED/EVIDENCE/ACTION message on a duplicate, fail-open on
anything it doesn't understand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_pkg"


def _gate(stdin: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CARTOGATE_REPO": str(FIXTURE), "CARTOGATE_REPO_ID": "t"}
    return subprocess.run(
        [sys.executable, "-m", "cartogate.writegate"],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_claude_shaped_payload_blocks_a_duplicate() -> None:
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"content": "def authenticate(name):\n    return 1\n"},
        }
    )
    result = _gate(payload)
    assert result.returncode == 2
    assert "BLOCKED:" in result.stderr and "ACTION:" in result.stderr


def test_windsurf_shaped_payload_is_autodetected_and_blocks() -> None:
    payload = json.dumps(
        {"tool_info": {"code": "def authenticate(name):\n    return 1\n", "file_path": "x.py"}}
    )
    result = _gate(payload)
    assert result.returncode == 2
    assert "BLOCKED:" in result.stderr


def test_cascade_edits_array_payload_blocks_a_duplicate() -> None:
    """Devin Desktop's Cascade `pre_write_code` nests the proposed code in an `edits` ARRAY
    (`tool_info.edits[].new_string`) — not at the tool_info root. Without this the installed
    Cascade gate extracts no text and never blocks (it fires, but is inert)."""
    payload = json.dumps(
        {
            "agent_action_name": "pre_write_code",
            "trajectory_id": "t-1",
            "tool_info": {
                "file_path": "x.py",
                "edits": [
                    {"old_string": "", "new_string": "def authenticate(name):\n    return 1\n"},
                ],
            },
        }
    )
    result = _gate(payload)
    assert result.returncode == 2
    assert "BLOCKED:" in result.stderr


def test_cascade_multi_edit_payload_gates_every_edit() -> None:
    """A multi-edit payload must be gated across ALL edits — a duplicate in the second edit
    is still a duplicate."""
    payload = json.dumps(
        {
            "agent_action_name": "pre_write_code",
            "tool_info": {
                "file_path": "x.py",
                "edits": [
                    {"new_string": "def totally_novel_first_edit(q):\n    return q\n"},
                    {"new_string": "def authenticate(name):\n    return 1\n"},
                ],
            },
        }
    )
    assert _gate(payload).returncode == 2


def test_cascade_edits_with_novel_code_passes() -> None:
    payload = json.dumps(
        {
            "agent_action_name": "pre_write_code",
            "tool_info": {
                "file_path": "x.py",
                "edits": [{"new_string": "def novel_cascade_symbol_abc(q):\n    return q\n"}],
            },
        }
    )
    assert _gate(payload).returncode == 0


def test_cascade_malformed_edits_fails_open() -> None:
    """A non-list `edits`, or entries that aren't dicts, must degrade to fail-open."""
    for edits in ("a string", [None, 42], [{"new_string": None}], []):
        payload = json.dumps(
            {"agent_action_name": "pre_write_code",
             "tool_info": {"file_path": "x.py", "edits": edits}}
        )
        assert _gate(payload).returncode == 0, edits


def test_novel_code_passes() -> None:
    payload = json.dumps(
        {"tool_input": {"content": "def definitely_novel_symbol_xyz(q):\n    return q\n"}}
    )
    assert _gate(payload).returncode == 0


def test_garbage_fails_open() -> None:
    assert _gate("not json at all").returncode == 0
    assert _gate(json.dumps(["not", "a", "dict"])).returncode == 0


def test_non_edit_tool_payload_passes() -> None:
    """A catch-all matcher runs the gate on EVERY tool call: non-edit tools (exec and friends)
    carry no proposed source, so the gate must fail open rather than guess."""
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "exec",
         "tool_input": {"command": "def authenticate(): pass"}}
    )
    assert _gate(payload).returncode == 0


def test_non_edit_tool_carrying_prose_in_a_risk_key_passes() -> None:
    """The catch-all matcher's blast radius, lower bound: the gate reads `content`/`text`/
    `new_string`/`new_str` from EVERY tool, but text has to parse into a real definition
    before it can collide. Ordinary prose in those keys is inert."""
    payload = json.dumps(
        {"tool_name": "web_search",
         "tool_input": {"text": "how do I authenticate a user in python"}}
    )
    assert _gate(payload).returncode == 0


def test_non_edit_tool_carrying_code_in_a_risk_key_blocks() -> None:
    """The catch-all matcher's blast radius, upper bound — PINNED, not endorsed.

    The gate decides from the PAYLOAD, not the tool name (Devin's edit-tool names are
    unpublished, and a wrong name-matcher yields an INERT gate — worse, because it still looks
    installed). The cost: any tool whose input carries duplicate-shaped source in one of those
    keys blocks, even when it isn't writing a file. Change this test deliberately when hooklog
    telemetry names the real edit tools and the matcher narrows (task #38)."""
    payload = json.dumps(
        {"tool_name": "web_search",
         "tool_input": {"content": "def authenticate(name):\n    return 1\n"}}
    )
    assert _gate(payload).returncode == 2


def test_module_import_stays_light() -> None:
    """With a catch-all matcher the gate spawns on every tool call, so importing the extractor
    (tree-sitter et al) at module scope would tax the whole agent loop. The heavy modules must
    load only once a payload actually carries code."""
    code = (
        "import sys, cartogate.writegate; "
        "print(any(m.startswith('cartogate.extract') or m.startswith('tree_sitter') "
        "for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "False", out.stdout


def test_fast_path_avoids_the_heavy_imports() -> None:
    """The no-source fast path must not import `cartogate.surfaces` (~290ms of the ~400ms a
    cold gate costs) — with a catch-all matcher that tax would land on EVERY tool call."""
    code = (
        "import sys, io, json; "
        "sys.stdin = io.StringIO(json.dumps({'tool_input': {'command': 'ls'}})); "
        "import cartogate.writegate as wg; rc = wg.main(); "
        "print(rc, 'cartogate.surfaces' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "0 False", (out.stdout, out.stderr)


def test_console_script_is_registered() -> None:
    from importlib.metadata import entry_points

    scripts = {ep.name for ep in entry_points(group="console_scripts")}
    assert "cartogate-write-gate" in scripts
