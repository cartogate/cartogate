"""Claude Code / Codex ``PreToolUse`` gate — thin shim over :mod:`cartogate.writegate` (F-13).

The gate logic lives in the installed package so ``cartogate init --agent <tool>`` can wire the
``cartogate-write-gate`` console script into any repo without copying scripts. This shim keeps
the historical repo-local entry point (and this repo's own hook config) working unchanged.
"""

from __future__ import annotations

from cartogate.writegate import (  # noqa: F401 — re-exported public surface
    EXIT_BLOCK,
    EXIT_OK,
    evaluate,
    file_path_of,
    main,
    run,
)

if __name__ == "__main__":
    raise SystemExit(main())
