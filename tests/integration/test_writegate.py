"""F-13 — the write-time gate as a FIRST-CLASS packaged surface (`cartogate.writegate`).

The research (docs/dev/STRATEGY.md law 2) says hooks are the only deterministic enforcement
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


def test_novel_code_passes() -> None:
    payload = json.dumps(
        {"tool_input": {"content": "def definitely_novel_symbol_xyz(q):\n    return q\n"}}
    )
    assert _gate(payload).returncode == 0


def test_garbage_fails_open() -> None:
    assert _gate("not json at all").returncode == 0
    assert _gate(json.dumps(["not", "a", "dict"])).returncode == 0


def test_console_script_is_registered() -> None:
    from importlib.metadata import entry_points

    scripts = {ep.name for ep in entry_points(group="console_scripts")}
    assert "cartogate-write-gate" in scripts
