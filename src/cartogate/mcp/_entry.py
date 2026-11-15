"""Console entry for ``cartogate-mcp``.

The MCP SDK is an optional extra (``cartogate[mcp]``). If it's missing, importing
:mod:`cartogate.mcp.server` (which imports the SDK at module top) fails with a raw
``ModuleNotFoundError`` traceback — unhelpful for someone who just wants to wire up the server. This
thin entry keeps the SDK import out of module scope so it can check first and exit with an
*actionable* message, then delegate to the real server only once the SDK is present.
"""

from __future__ import annotations

import importlib.util
import sys

#: The SDK range the server is written against — keep in sync with pyproject's `mcp` pin.
_SDK_SPEC = "mcp>=2,<3"

_MISSING_SDK_HINT = (
    "cartogate-mcp needs the MCP SDK, which isn't installed.\n"
    "Install it, then retry:\n"
    f"  pipx:  pipx inject cartogate '{_SDK_SPEC}'\n"
    "  pip:   pip install 'cartogate[mcp]'\n"
    "(The gate and the rest of the `cartogate` CLI work fine without it.)\n"
)

_WRONG_SDK_HINT = (
    "cartogate-mcp needs MCP SDK 2.x, but version {found} is installed.\n"
    "{why}\n"
    "Install a supported version, then retry:\n"
    f"  pipx:  pipx inject --force cartogate '{_SDK_SPEC}'\n"
    f"  pip:   pip install --upgrade '{_SDK_SPEC}'\n"
)

_TOO_OLD = "2.0 replaced the handler-registration API this server is built on, so 1.x can't run it."
_TOO_NEW = (
    "That major version is newer than this server was written against, and may have moved\n"
    "the handler-registration API again."
)


def _installed_mcp_version() -> str | None:
    """The installed SDK's version string, or ``None`` if the metadata can't be read."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mcp")
    except PackageNotFoundError:
        return None


def _major_of(version_str: str | None) -> int | None:
    """The major component of a version string, or ``None`` when it isn't a plain integer.

    ``None`` deliberately means "let it through": a version we can't parse (an epoch like
    ``1!2.0.0``, a vendored build, absent metadata) is far more likely to be a working install
    than a broken one, and a false positive here BLOCKS a server that would have run fine.
    """
    if not version_str:
        return None
    head = version_str.split(".")[0]
    # isdecimal, not isdigit: isdigit accepts superscripts like '²', on which int() then raises.
    return int(head) if head.isdecimal() else None


def main() -> None:
    """Fail clearly if the MCP SDK is absent or unsupported, else hand off to the stdio server."""
    if importlib.util.find_spec("mcp") is None:
        sys.stderr.write(_MISSING_SDK_HINT)
        raise SystemExit(1)
    # Presence alone isn't enough: a 1.x SDK satisfies find_spec but lacks `add_request_handler`,
    # so the import below would die with a raw traceback — the "dead MCP server" failure class
    # this entry exists to turn into an actionable message. Both bounds are checked, matching
    # the declared range: a 3.x SDK could move that API again just as 2.0 did.
    installed = _installed_mcp_version()
    major = _major_of(installed)
    if major is not None and not 2 <= major < 3:
        why = _TOO_OLD if major < 2 else _TOO_NEW
        sys.stderr.write(_WRONG_SDK_HINT.format(found=installed, why=why))
        raise SystemExit(1)
    from cartogate.mcp.server import main as serve

    serve()


if __name__ == "__main__":
    main()
