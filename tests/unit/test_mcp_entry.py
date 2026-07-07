"""The `cartogate-mcp` console entry fails clearly when the optional MCP SDK is missing, and
delegates to the real stdio server when it's present.
"""

from __future__ import annotations

import pytest

from cartogate.mcp import _entry


def test_missing_mcp_sdk_exits_with_actionable_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(SystemExit) as exc_info:
        _entry.main()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "pipx inject cartogate" in err  # the actionable fix, not a raw traceback
    assert "cartogate[mcp]" in err


def test_present_mcp_sdk_delegates_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = {"name": ""}

    def _find_spec(name: str) -> object:  # pretend the SDK is installed, and record what was probed
        probed["name"] = name
        return object()

    monkeypatch.setattr("importlib.util.find_spec", _find_spec)
    import cartogate.mcp.server as server

    called = {"served": False}
    monkeypatch.setattr(server, "main", lambda: called.__setitem__("served", True))
    _entry.main()
    assert called["served"] is True  # handed off to the stdio server
    assert probed["name"] == "mcp"  # probed the right module
