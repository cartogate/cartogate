"""Windsurf Cascade ``pre_write_code`` gate — thin shim over :mod:`cartogate.writegate` (F-13).

The shared gate auto-detects Windsurf's ``tool_info`` payload shape (see
``cartogate.writegate.normalize``), so this shim just delegates. Kept so an existing
``.windsurf/hooks.json`` pointing at this script keeps working; new installs use the
``cartogate-write-gate`` console script that ``cartogate init --agent windsurf`` wires in.
"""

from __future__ import annotations

from cartogate.writegate import (  # noqa: F401 — re-exported public surface
    EXIT_BLOCK,
    EXIT_OK,
    main,
    normalize,
)

if __name__ == "__main__":
    raise SystemExit(main())
