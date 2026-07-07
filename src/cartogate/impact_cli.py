"""``cartogate impact`` — a PR-time impact summary from a git diff (F-68).

Indexes a tree, diffs it (working tree, ``--staged``, or against a ``--ref``), maps the changed
lines to symbols, and prints the composed impact summary (affected code + tests to run + docs to
review) as Markdown or JSON. Advisory — it reports, it never fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cartogate.engine.diff import git_diff_regions
from cartogate.engine.impact import build_impact_summary, changed_symbol_qnames
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def cmd_impact(
    root: Path,
    *,
    ref: str | None = None,
    staged: bool = False,
    depth: int = 1,
    as_json: bool = False,
) -> int:
    """Index ``root``, diff it, and print the impact summary. Paths align when ``root`` is the
    repo root (git diff and the index both use repo-relative paths)."""
    root = root.resolve()
    store = InMemoryStore()
    try:
        index_package(root, repo_id=root.name, store=store, base=root)
    except Exception as exc:  # CLI boundary: a clean message beats a traceback
        print(f"cartogate impact: failed to index {root}: {exc}", file=sys.stderr)
        return 1
    try:
        regions = git_diff_regions(root, staged=staged, ref=ref)
    except (RuntimeError, OSError) as exc:  # git failed, bad ref, or git not on PATH
        print(f"cartogate impact: {exc}", file=sys.stderr)
        return 1

    summary = build_impact_summary(store, changed_symbol_qnames(store, regions), depth=depth)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # report is UTF-8; avoid a cp1252 console crash
    print(json.dumps(summary.to_dict(), indent=2) if as_json else summary.to_markdown())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cartogate impact",
        description="PR-time impact summary (affected code + tests to run + docs to review).",
    )
    parser.add_argument("root", nargs="?", default=".", help="repo/source root to index")
    parser.add_argument(
        "--ref", default=None, help="diff against this commit/range (e.g. origin/main)"
    )
    parser.add_argument(
        "--staged", action="store_true", help="diff the index against HEAD (pre-commit)"
    )
    parser.add_argument("--depth", type=int, default=1, help="reverse-reachability depth")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown"
    )
    args = parser.parse_args(argv)
    return cmd_impact(
        Path(args.root), ref=args.ref, staged=args.staged, depth=args.depth, as_json=args.as_json
    )


if __name__ == "__main__":
    raise SystemExit(main())
