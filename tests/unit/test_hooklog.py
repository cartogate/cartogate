"""Gate tests for the hook firing log (Devin Desktop instrumentation, PR A).

Two pieces under test:

- ``cartogate-hook log --source <tag>`` (``cartogate.hooklog``): the universal logging
  dispatcher wired into every plausible hook location. It appends one JSONL line per
  invocation to ``.cartogate/hooklog.jsonl`` recording WHICH surface fired and WHICH agent
  dialect the payload spoke — and it must NEVER disturb the agent (exit 0 always, silent
  stdout), because a broken logging hook would train the agent to route around Cartogate.
- ``cartogate hooks status`` (``cartogate.hooks_cli``): the reader that cross-references
  installed hook entries against the firing log, so the user can empirically determine
  which of the shotgun-installed surfaces their Devin Desktop build actually reads.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from cartogate import hooklog


def _invoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdin: str, argv: list[str]
) -> int:
    """Run the dispatcher as the console script would: cwd = workspace, payload on stdin."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return hooklog.main(argv)


def _lines(tmp_path: Path) -> list[dict[str, object]]:
    text = (tmp_path / ".cartogate" / "hooklog.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


CASCADE_PAYLOAD = json.dumps(
    {
        "agent_action_name": "post_write_code",
        "trajectory_id": "traj-1",
        "execution_id": "exec-1",
        "tool_info": {"file_path": "src/app.py", "edits": [{"old_string": "a", "new_string": "b"}]},
    }
)

DEVIN_PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "str_replace",
        "tool_input": {"file_path": "src/app.py", "new_string": "x"},
        "session_id": "sess-1",
    }
)


class TestDispatcher:
    def test_cascade_payload_logged_with_dialect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc = _invoke(
            monkeypatch, tmp_path, CASCADE_PAYLOAD,
            ["log", "--source", ".devin/hooks.json:post_write_code"],
        )
        assert rc == 0
        (line,) = _lines(tmp_path)
        assert line["source"] == ".devin/hooks.json:post_write_code"
        assert line["dialect"] == "cascade"
        assert line["event"] == "post_write_code"
        assert line["file"] == "src/app.py"
        assert line["session"] == "traj-1"
        assert "ts" in line
        # Never the payload body itself — paths and names only (no code, no prompts).
        assert "new_string" not in json.dumps(line)

    def test_devin_payload_logged_with_tool_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rc = _invoke(
            monkeypatch, tmp_path, DEVIN_PAYLOAD,
            ["log", "--source", ".devin/hooks.v1.json:PreToolUse"],
        )
        assert rc == 0
        (line,) = _lines(tmp_path)
        assert line["dialect"] == "devin"
        assert line["event"] == "PreToolUse"
        assert line["tool"] == "str_replace"
        assert line["file"] == "src/app.py"
        assert line["session"] == "sess-1"
        assert "new_string" not in json.dumps(line)  # provenance only, never edit content

    def test_undecodable_stdin_still_logs_the_firing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stdin read that itself fails (non-UTF-8 payload) must still leave firing
        evidence — the reason the hook ran matters more than what it carried."""
        def bad_read() -> str:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "read", bad_read)
        assert hooklog.main(["log", "--source", "x:y"]) == 0
        (line,) = _lines(tmp_path)
        assert line["dialect"] == "unparseable"

    def test_unparseable_stdin_still_logs_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Firing evidence matters even when the payload is garbage — log the firing, exit 0."""
        rc = _invoke(monkeypatch, tmp_path, "not json {{", ["log", "--source", "x:y"])
        assert rc == 0
        (line,) = _lines(tmp_path)
        assert line["dialect"] == "unparseable"
        assert line["source"] == "x:y"

    def test_unknown_dict_shape_logs_its_keys(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A new/unknown payload schema is diagnosable from its top-level key names."""
        rc = _invoke(
            monkeypatch, tmp_path, json.dumps({"mystery": 1, "shape": 2}),
            ["log", "--source", "x:y"],
        )
        assert rc == 0
        (line,) = _lines(tmp_path)
        assert line["dialect"] == "unknown"
        assert "mystery" in line["keys"]

    def test_append_failure_never_blocks_the_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(hooklog, "append", boom)
        rc = _invoke(monkeypatch, tmp_path, DEVIN_PAYLOAD, ["log", "--source", "x:y"])
        assert rc == 0

    def test_silent_on_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Devin hooks parse stdout as JSON directives — the logger must never write there."""
        _invoke(monkeypatch, tmp_path, DEVIN_PAYLOAD, ["log", "--source", "x:y"])
        assert capsys.readouterr().out == ""

    def test_appends_across_invocations(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _invoke(monkeypatch, tmp_path, DEVIN_PAYLOAD, ["log", "--source", "a:b"])
        _invoke(monkeypatch, tmp_path, CASCADE_PAYLOAD, ["log", "--source", "c:d"])
        assert [ln["source"] for ln in _lines(tmp_path)] == ["a:b", "c:d"]

    def test_unknown_action_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Even a miswired entry (wrong argv) must not disturb the agent."""
        rc = _invoke(monkeypatch, tmp_path, "", ["frobnicate"])
        assert rc == 0


class TestHooksStatus:
    def _install(self, tmp_path: Path) -> None:
        """A representative shotgun install: Devin-schema v1 file + Cascade-schema file."""
        (tmp_path / ".devin").mkdir()
        (tmp_path / ".devin" / "hooks.v1.json").write_text(
            json.dumps({
                "PreToolUse": [
                    {"matcher": "Write|Edit",
                     "hooks": [{"type": "command", "command": "cartogate-write-gate"}]},
                    {"matcher": "",
                     "hooks": [{"type": "command",
                                "command": "cartogate-hook log --source "
                                           ".devin/hooks.v1.json:PreToolUse"}]},
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "cartogate-hook log --source "
                                           ".devin/hooks.v1.json:SessionStart"}]},
                ],
            }),
            encoding="utf-8",
        )
        (tmp_path / ".devin" / "hooks.json").write_text(
            json.dumps({
                "hooks": {
                    "post_write_code": [
                        {"command": "cartogate-hook log --source .devin/hooks.json:post_write_code",
                         "powershell": "cartogate-hook log --source "
                                       ".devin/hooks.json:post_write_code",
                         "show_output": False},
                    ],
                }
            }),
            encoding="utf-8",
        )

    def test_status_reports_fired_and_silent_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cartogate.hooks_cli import main

        self._install(tmp_path)
        (tmp_path / ".cartogate").mkdir()
        log = tmp_path / ".cartogate" / "hooklog.jsonl"
        log.write_text(
            json.dumps({"ts": "2026-07-27T10:00:00+00:00", "dialect": "devin",
                        "source": ".devin/hooks.v1.json:PreToolUse",
                        "event": "PreToolUse", "tool": "str_replace"}) + "\n"
            + json.dumps({"ts": "2026-07-27T10:01:00+00:00", "dialect": "devin",
                          "source": ".devin/hooks.v1.json:PreToolUse",
                          "event": "PreToolUse", "tool": "exec"}) + "\n",
            encoding="utf-8",
        )
        assert main(["status", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert ".devin/hooks.v1.json:PreToolUse" in out
        assert "2" in out  # fired count
        assert "never fired" in out  # the silent entries (SessionStart, post_write_code)
        assert "cartogate-write-gate" in out  # gate entries listed as installed

    def test_status_with_no_log_lists_installed_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cartogate.hooks_cli import main

        self._install(tmp_path)
        assert main(["status", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "no hook firings recorded" in out
        assert ".devin/hooks.json" in out

    def test_status_survives_truncated_source_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hand-edited entry ending at `--source ` (marker present, tag missing) must not
        crash the status report (review finding: unguarded split indexing)."""
        from cartogate.hooks_cli import main

        (tmp_path / ".devin").mkdir()
        (tmp_path / ".devin" / "hooks.v1.json").write_text(
            json.dumps({
                "SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "cartogate-hook log --source "}]},
                ],
            }),
            encoding="utf-8",
        )
        assert main(["status", str(tmp_path)]) == 0
        assert "cartogate-hook log --source" in capsys.readouterr().out

    def test_status_survives_malformed_hook_configs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cartogate.hooks_cli import main

        (tmp_path / ".devin").mkdir()
        (tmp_path / ".devin" / "hooks.v1.json").write_text("not json {{", encoding="utf-8")
        (tmp_path / ".devin" / "hooks.json").write_text(json.dumps([1, 2]), encoding="utf-8")
        assert main(["status", str(tmp_path)]) == 0

    def test_status_reports_devin_tool_names_seen(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The catch-all PreToolUse log is how we learn Devin's real edit tool names (task
        #38) — status must surface the distinct tool names observed per dialect."""
        from cartogate.hooks_cli import main

        self._install(tmp_path)
        (tmp_path / ".cartogate").mkdir()
        (tmp_path / ".cartogate" / "hooklog.jsonl").write_text(
            json.dumps({"ts": "2026-07-27T10:00:00+00:00", "dialect": "devin",
                        "source": ".devin/hooks.v1.json:PreToolUse",
                        "event": "PreToolUse", "tool": "str_replace"}) + "\n",
            encoding="utf-8",
        )
        main(["status", str(tmp_path)])
        assert "str_replace" in capsys.readouterr().out
