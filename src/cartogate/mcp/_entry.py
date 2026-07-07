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

_MISSING_SDK_HINT = (
    "cartogate-mcp needs the MCP SDK, which isn't installed.\n"
    "Install it, then retry:\n"
    "  pipx:  pipx inject cartogate 'mcp>=1.2,<2'\n"
    "  pip:   pip install 'cartogate[mcp]'\n"
    "(The gate and the rest of the `cartogate` CLI work fine without it.)\n"
)


def main() -> None:
    """Fail clearly if the MCP SDK is absent, else hand off to the stdio server."""
    if importlib.util.find_spec("mcp") is None:
        sys.stderr.write(_MISSING_SDK_HINT)
        raise SystemExit(1)
    from cartogate.mcp.server import main as serve

    serve()


if __name__ == "__main__":
    main()
