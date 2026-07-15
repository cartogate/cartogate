"""Tests for init wiring of the grep nudge."""

from __future__ import annotations

import json
from pathlib import Path

from cartogate.init_cmd import RULE_NUDGE, _install_grep_nudge_claude, _Report


def _read_settings(root: Path) -> dict[str, object]:
    return json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_grep_nudge_hook_installed(tmp_path: Path) -> None:
    report = _Report(False)

    _install_grep_nudge_claude(tmp_path, report)

    data = _read_settings(tmp_path)
    entries = data["hooks"]["PreToolUse"]
    assert any(
        entry["matcher"] == "Grep"
        and entry["hooks"][0]["command"] == "cartogate-grep-nudge"
        for entry in entries
    )


def test_grep_nudge_idempotent(tmp_path: Path) -> None:
    report = _Report(False)

    _install_grep_nudge_claude(tmp_path, report)
    _install_grep_nudge_claude(tmp_path, report)

    data = _read_settings(tmp_path)
    entries = data["hooks"]["PreToolUse"]
    ours = [
        entry for entry in entries if "cartogate-grep-nudge" in json.dumps(entry)
    ]
    assert len(ours) == 1


def test_existing_hooks_preserved(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [
                                {"type": "command", "command": "my-own-hook"}
                            ],
                        }
                    ]
                },
                "other": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    _install_grep_nudge_claude(tmp_path, _Report(False))

    data = _read_settings(tmp_path)
    entries = data["hooks"]["PreToolUse"]
    assert any("my-own-hook" in json.dumps(entry) for entry in entries)
    assert any("cartogate-grep-nudge" in json.dumps(entry) for entry in entries)
    assert data["other"] == {"keep": True}


def test_rule_has_grep_decision() -> None:
    text = RULE_NUDGE.lower()

    assert "grep" in text
    assert "find_references" in RULE_NUDGE
    assert "repo_map" in RULE_NUDGE
    assert "file:line" in RULE_NUDGE


def test_block_recovery_preserved() -> None:
    assert "--no-verify" in RULE_NUDGE
    assert RULE_NUDGE.rstrip().endswith("never bypass with `--no-verify`.")
