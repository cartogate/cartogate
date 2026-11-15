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


@pytest.mark.parametrize(
    ("installed", "expected_reason"),
    [("1.28.1", "1.x can't run it"), ("3.0.0", "newer than this server")],
)
def test_unsupported_mcp_sdk_exits_with_an_actionable_hint(
    installed: str,
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Presence alone isn't enough: an out-of-range SDK satisfies find_spec but may not have the
    handler-registration API this server is built on, so importing it would die with a raw
    traceback — the exact 'dead MCP server' failure this entry exists to turn into a message.
    Both bounds are checked, because 3.x could move that API again just as 2.0 did."""
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(_entry, "_installed_mcp_version", lambda: installed)
    with pytest.raises(SystemExit) as exc_info:
        _entry.main()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert installed in err  # says what's actually installed
    assert "mcp>=2,<3" in err  # and the range that works
    assert expected_reason in err  # and WHY this one doesn't


@pytest.mark.parametrize("installed", [None, "", "1!2.0.0", "weird", "²"])
def test_unparseable_sdk_version_is_let_through(
    installed: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version we can't parse must NOT block: absent metadata, an epoch, or a vendored build is
    far likelier to be a working install than a broken one, and a false positive here kills a
    server that would have run fine. ('²' is isdigit-but-not-isdecimal — int() would raise.)"""
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr(_entry, "_installed_mcp_version", lambda: installed)
    import cartogate.mcp.server as server

    called = {"served": False}
    monkeypatch.setattr(server, "main", lambda: called.__setitem__("served", True))
    _entry.main()
    assert called["served"] is True


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
