"""Git pre-commit gate — thin shim over :mod:`cartogate.precommit`.

The full gate (diff-aware duplicate blocking + reference-integrity advisory + pass stamping)
lives in the installed package; this repo-local script simply delegates so both entry points
behave identically. Install via ``cartogate init --agent <tool>`` (preferred) or by copying
``hooks/pre-commit`` into ``.git/hooks/``.
"""

from __future__ import annotations

import sys

from cartogate.precommit import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
