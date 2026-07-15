"""Tests for grepnudge — PreToolUse advisory hook for the Grep tool."""

import json
from pathlib import Path

from cartogate.grepnudge import evaluate, extract_pattern


class TestExtractPattern:
    """Tests for pattern extraction from tool payloads."""

    def test_grep_tool_extracts_pattern(self):
        """Grep tool input carries pattern directly."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "load_config", "glob": "**/*.py"}}
        assert extract_pattern(payload) == "load_config"

    def test_bash_payload_ignored(self):
        """Bash is deliberately not hooked — a Bash payload extracts nothing."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn UserStore src/"},
        }
        assert extract_pattern(payload) is None

    def test_non_grep_tool_returns_none(self):
        """Write, Read, other tools return None."""
        payload = {"tool_name": "Write", "tool_input": {"file_path": "test.py"}}
        assert extract_pattern(payload) is None

    def test_missing_tool_input_returns_none(self):
        """Payload without tool_input returns None."""
        payload = {"tool_name": "Grep"}
        assert extract_pattern(payload) is None


class TestEvaluate:
    """Tests for the advisory nudge logic."""

    def test_symbol_grep_nudges(self):
        """Symbol pattern triggers advisory context about cartogate."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "load_config"}}
        result = evaluate(payload)
        assert result is not None
        assert "hookSpecificOutput" in result
        output = result["hookSpecificOutput"]
        assert output["hookEventName"] == "PreToolUse"
        assert "find_references" in output["additionalContext"]
        assert "find_symbol" in output["additionalContext"]
        assert "load_config" in output["additionalContext"]

    def test_dotted_symbol_nudges(self):
        """Qualified name (with dots) qualifies as symbol."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "auth.login"}}
        result = evaluate(payload)
        assert result is not None
        assert "find_references" in result["hookSpecificOutput"]["additionalContext"]

    def test_regex_pattern_silent(self):
        """Regex metachars prevent nudging."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "def .*load"}}
        assert evaluate(payload) is None

    def test_pattern_with_spaces_silent(self):
        """Patterns with spaces don't nudge."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "class MyClass:"}}
        assert evaluate(payload) is None

    def test_short_pattern_silent(self):
        """Patterns < 3 chars are too generic."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "x"}}
        assert evaluate(payload) is None

    def test_bash_payload_silent(self):
        """Bash is not hooked — even a symbol-shaped grep command in Bash produces no nudge."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -rn UserStore src/"},
        }
        assert evaluate(payload) is None

    def test_other_tools_silent(self):
        """Write, Read, etc. don't nudge."""
        payload = {"tool_name": "Write", "tool_input": {"file_path": "test.py"}}
        assert evaluate(payload) is None

    def test_nudge_mentions_cartogate_tools(self):
        """Nudge message lists available cartogate tools."""
        payload = {"tool_name": "Grep", "tool_input": {"pattern": "load_config"}}
        result = evaluate(payload)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "find_references" in ctx
        assert "find_symbol" in ctx
        assert "blast_radius" in ctx
        assert "implementations" in ctx


class TestMain:
    """Tests for the entry point."""

    def test_main_never_blocks(self, monkeypatch, capsys):
        """main() always exits 0, even on garbage input."""
        # Simulate garbage stdin
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
        from cartogate.grepnudge import main

        exit_code = main()
        assert exit_code == 0
        # Should print nothing
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_main_prints_nudge_on_symbol(self, monkeypatch, capsys):
        """main() prints JSON output for symbol patterns."""
        import io

        payload = {"tool_name": "Grep", "tool_input": {"pattern": "load_config"}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        from cartogate.grepnudge import main

        exit_code = main()
        assert exit_code == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "hookSpecificOutput" in output


class TestConsoleScript:
    """Test that the console script is registered."""

    def test_console_script_registered(self):
        """cartogate-grep-nudge is in pyproject.toml [project.scripts]."""
        pyproject_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        content = pyproject_path.read_text()
        assert 'cartogate-grep-nudge = "cartogate.grepnudge:main"' in content
