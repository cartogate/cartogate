"""Top-level ``cartogate`` CLI router.

Thin dispatcher so ``cartogate viz`` and ``cartogate daemon`` coexist: ``viz`` goes to the viz
CLI; everything else is handed to the daemon CLI (which owns ``daemon start|stop|status``). The
daemon's detached self-spawn invokes ``cartogate.daemon.cli`` directly and is unaffected.
"""

from __future__ import annotations

import sys

_USAGE = (
    "usage: cartogate {init,daemon,index,hooks,doctor,stats,viz,impact,localize,cfg,slice} ..."
    " (--version)\n\n"
    "  init      set up Cartogate here (MCP + daemon); --agent <tool> adds rules + commit gate\n"
    "  daemon    manage the warm gate daemon (start|stop|status)\n"
    "  index     build/refresh the resolved graph snapshot (fast cold starts; F-09)\n"
    "  hooks     install git hooks that refresh the snapshot on commit/merge/checkout\n"
    "  doctor    check Cartogate is healthy: daemon, live gate probe, hook wiring\n"
    "  stats     what Cartogate knows about this repo + duplicates it has prevented\n"
    "  viz       export the code graph (GraphML / JSON / interactive HTML)\n"
    "  impact    PR-time impact summary from a git diff (affected code + tests + docs)\n"
    "  localize  rank likely culprits behind a failing test from a git diff\n"
    "  cfg       control-flow analysis: statement-level unreachable code (Py/JS/TS/Go/Java/C)\n"
    "  slice     program slice: statements affecting (or affected by) a line (Py/JS/TS/Go/Java/C)\n"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    if args[0] in ("-V", "--version"):
        from cartogate import __version__

        print(f"cartogate {__version__}")
        return 0
    if args[0] == "viz":
        from cartogate.viz.cli import main as viz_main

        return viz_main(args[1:])
    if args[0] == "impact":
        from cartogate.impact_cli import main as impact_main

        return impact_main(args[1:])
    if args[0] == "localize":
        from cartogate.localize_cli import main as localize_main

        return localize_main(args[1:])
    if args[0] == "cfg":
        from cartogate.cfg_cli import main as cfg_main

        return cfg_main(args[1:])
    if args[0] == "slice":
        from cartogate.slice_cli import main as slice_main

        return slice_main(args[1:])
    if args[0] == "hooks":
        from cartogate.hooks_cli import main as hooks_main

        return hooks_main(args[1:])
    if args[0] == "index":
        from cartogate.index_cli import main as index_main

        return index_main(args[1:])
    if args[0] == "init":
        from cartogate.init_cmd import main as init_main

        return init_main(args[1:])
    if args[0] == "doctor":
        from cartogate.doctor import main as doctor_main

        return doctor_main(args[1:])
    if args[0] == "stats":
        from cartogate.stats import main as stats_main

        return stats_main(args[1:])
    if args[0] == "daemon":
        from cartogate.daemon.cli import main as daemon_main

        return daemon_main(args)
    print(f"cartogate: unknown command {args[0]!r}\n\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
