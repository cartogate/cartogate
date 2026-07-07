"""Section 4 — the enforcement hooks driven as real subprocesses (exit-code contract)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_pkg"


def _run(script: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_pre_commit_blocks_duplicate_signatures(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (pkg / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    result = _run(HOOKS / "pre_commit.py", str(pkg))
    assert result.returncode == 1
    assert "BLOCKED:" in result.stderr
    assert "EVIDENCE (EXTRACTED):" in result.stderr and ".py:" in result.stderr
    assert "ACTION:" in result.stderr and "Do NOT retry" in result.stderr


def test_pre_commit_passes_clean_repo() -> None:
    result = _run(HOOKS / "pre_commit.py", str(FIXTURE))
    assert result.returncode == 0


def test_pretooluse_blocks_existing_function() -> None:
    import os

    env = {**os.environ, "CARTOGATE_REPO": str(FIXTURE), "CARTOGATE_REPO_ID": "t"}
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"content": "def authenticate(name):\n    return 1\n"},
        }
    )
    result = _run(HOOKS / "pretooluse_gate.py", stdin=payload, env=env)
    assert result.returncode == 2  # blocked
    assert "BLOCKED:" in result.stderr
    # STRATEGY.md law 1 — the full shape that converts a block into self-correction:
    assert "EVIDENCE (EXTRACTED):" in result.stderr
    assert "ACTION:" in result.stderr and "reuse" in result.stderr
    assert "Do NOT retry" in result.stderr and "rename" in result.stderr


def test_pretooluse_allows_novel_function() -> None:
    import os

    env = {**os.environ, "CARTOGATE_REPO": str(FIXTURE), "CARTOGATE_REPO_ID": "t"}
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"content": "def compute_tax(x, y, z):\n    return x\n"},
        }
    )
    result = _run(HOOKS / "pretooluse_gate.py", stdin=payload, env=env)
    assert result.returncode == 0


def test_pretooluse_allows_editing_the_symbols_own_file() -> None:
    # Re-writing `authenticate` IN THE FILE THAT DEFINES IT is an edit, not a duplicate — the gate
    # must not self-block (F-28). End-to-end check that the editing-unit matching works. (No daemon
    # runs here, so this exercises the in-process path; the daemon path shares the same
    # _editing_unit logic, forwarding exclude_unit over the socket.)
    import os

    env = {**os.environ, "CARTOGATE_REPO": str(FIXTURE), "CARTOGATE_REPO_ID": "t"}
    edit = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(FIXTURE / "auth.py"),  # the file where authenticate lives
                "new_string": "def authenticate(name):\n    return 1\n",
            },
        }
    )
    # not self-blocked — editing the symbol's own file:
    assert _run(HOOKS / "pretooluse_gate.py", stdin=edit, env=env).returncode == 0

    # ...but writing that same function into a DIFFERENT file is a real duplicate -> blocked.
    elsewhere = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(FIXTURE / "elsewhere.py"),
                "content": "def authenticate(name):\n    return 1\n",
            },
        }
    )
    assert _run(HOOKS / "pretooluse_gate.py", stdin=elsewhere, env=env).returncode == 2  # blocked


def test_pretooluse_ignores_non_json_stdin() -> None:
    result = _run(HOOKS / "pretooluse_gate.py", stdin="not json at all")
    assert result.returncode == 0


# --- Windsurf `pre_write_code` adapter ------------------------------------------------------
# Windsurf nests the edit under `tool_info` and blocks the write on exit code 2 (same contract
# as Claude Code's PreToolUse). The adapter normalizes that payload and reuses the shared gate,
# so these mirror the PreToolUse cases above on Windsurf's payload shape.


def _windsurf_env() -> dict[str, str]:
    import os

    return {**os.environ, "CARTOGATE_REPO": str(FIXTURE), "CARTOGATE_REPO_ID": "t"}


def test_windsurf_blocks_existing_function() -> None:
    payload = json.dumps(
        {
            "tool_info": {
                "file_path": str(FIXTURE / "elsewhere.py"),
                "code": "def authenticate(name):\n    return 1\n",
            }
        }
    )
    result = _run(HOOKS / "windsurf_gate.py", stdin=payload, env=_windsurf_env())
    assert result.returncode == 2  # blocked
    assert "BLOCKED:" in result.stderr and "ACTION:" in result.stderr


def test_windsurf_allows_novel_function() -> None:
    payload = json.dumps(
        {
            "tool_info": {
                "file_path": str(FIXTURE / "new_mod.py"),
                "code": "def compute_tax(x, y, z):\n    return x\n",
            }
        }
    )
    result = _run(HOOKS / "windsurf_gate.py", stdin=payload, env=_windsurf_env())
    assert result.returncode == 0


def test_windsurf_allows_editing_the_symbols_own_file() -> None:
    # Re-writing `authenticate` in the file that defines it is an edit, not a duplicate (F-28).
    edit = json.dumps(
        {
            "tool_info": {
                "file_path": str(FIXTURE / "auth.py"),
                "code": "def authenticate(name):\n    return 1\n",
            }
        }
    )
    assert _run(HOOKS / "windsurf_gate.py", stdin=edit, env=_windsurf_env()).returncode == 0


def test_windsurf_ignores_non_json_stdin() -> None:
    result = _run(HOOKS / "windsurf_gate.py", stdin="not json at all")
    assert result.returncode == 0


def test_gate_warns_loudly_when_the_daemon_crashed(tmp_path: Path) -> None:
    # A discovery file with a dead pid means a daemon was started but died. The gate must still
    # work (in-process) AND say so — a silent fallback would hide that the warm gate is gone.
    import json
    import os

    repo = tmp_path / "proj"
    (repo / ".cartogate").mkdir(parents=True)
    (repo / ".git").mkdir()  # so the gate auto-detects THIS repo (not the cwd)
    (repo / "auth.py").write_text("def authenticate(name):\n    return True\n", encoding="utf-8")
    (repo / ".cartogate" / "daemon.json").write_text(
        json.dumps(
            {"host": "127.0.0.1", "port": 1, "pid": 999_999, "token": "x", "repo": str(repo)}
        ),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "CARTOGATE_REPO"}
    payload = json.dumps(
        {
            "tool_info": {
                "file_path": str(repo / "elsewhere.py"),
                "code": "def authenticate(name):\n    return 1\n",
            }
        }
    )
    result = _run(HOOKS / "windsurf_gate.py", stdin=payload, env=env)
    assert result.returncode == 2  # still gates the duplicate via the in-process fallback
    assert "daemon unreachable" in result.stderr  # ...and warns the dev the warm gate is down
    assert "cartogate doctor" in result.stderr


def test_windsurf_autodetects_repo_from_edited_file(tmp_path: Path) -> None:
    # No CARTOGATE_REPO pinned: the gate must discover the repo from the file being written
    # (its nearest `.git` ancestor), so one global hook config works across every repo.
    import os

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "auth.py").write_text("def authenticate(name):\n    return True\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k not in ("CARTOGATE_REPO", "CARTOGATE_REPO_ID")}
    payload = json.dumps(
        {
            "tool_info": {
                "file_path": str(repo / "elsewhere.py"),  # a DIFFERENT file in the same repo
                "code": "def authenticate(name):\n    return 1\n",
            }
        }
    )
    result = _run(HOOKS / "windsurf_gate.py", stdin=payload, env=env)
    assert result.returncode == 2  # duplicate found in the auto-detected repo
    assert "BLOCKED:" in result.stderr and "ACTION:" in result.stderr
