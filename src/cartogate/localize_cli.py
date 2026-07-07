"""``cartogate localize`` — rank likely culprits behind a failing test, from a git diff (F-02).

Indexes a tree, derives the change (working tree, ``--staged``, or a ``--ref`` range), and ranks
the symbols the failing test exercises that the change touched — nearest first. Using ``--ref
origin/main`` makes this work for a *committed* cause on a branch, not just uncommitted edits.
Advisory — it suggests where to look, it never fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cartogate.engine.diff import git_diff_regions
from cartogate.engine.localize import (
    DEFAULT_MAX_DEPTH,
    localize,
    refine_with_cfg,
    refine_with_pdg,
)
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def cmd_localize(
    root: Path,
    test: str,
    *,
    ref: str | None = None,
    staged: bool = False,
    depth: int = DEFAULT_MAX_DEPTH,
    as_json: bool = False,
) -> int:
    """Index ``root``, diff it, and rank culprits for ``test``. Paths align when ``root`` is the
    repo root (git diff and the index both use repo-relative paths)."""
    root = root.resolve()
    store = InMemoryStore()
    try:
        index_package(root, repo_id=root.name, store=store, base=root)
    except Exception as exc:  # CLI boundary: a clean message beats a traceback
        print(f"cartogate localize: failed to index {root}: {exc}", file=sys.stderr)
        return 1
    try:
        regions = git_diff_regions(root, staged=staged, ref=ref)
    except (RuntimeError, OSError) as exc:  # git failed, bad ref, or git not on PATH
        print(f"cartogate localize: {exc}", file=sys.stderr)
        return 1

    report = localize(store, test, regions, max_depth=depth)

    def _read(rel_path: str) -> bytes | None:
        target = root / rel_path
        try:
            return target.read_bytes()
        except OSError:
            return None

    # Statement-level refinement (F-03): drop dead-code-change suspects, annotate reachable lines,
    # then flag changed lines that reach an observable output (PDG slice) and demote no-op changes.
    report = refine_with_cfg(report, regions, _read)
    report = refine_with_pdg(report, regions, _read)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # report is UTF-8; avoid a cp1252 console crash
    print(json.dumps(report.to_dict(), indent=2) if as_json else report.to_markdown())
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args and dispatch to ``cmd_localize``."""
    parser = argparse.ArgumentParser(
        prog="cartogate localize",
        description="Rank likely culprits behind a failing test (the code it runs that changed).",
    )
    parser.add_argument("test", help="qualified name of the failing test (e.g. pkg.mod.test_x)")
    parser.add_argument("root", nargs="?", default=".", help="repo/source root to index")
    parser.add_argument(
        "--ref", default=None, help="diff against this commit/range (e.g. origin/main)"
    )
    parser.add_argument(
        "--staged", action="store_true", help="diff the index against HEAD (pre-commit)"
    )
    parser.add_argument(
        "--depth", type=int, default=DEFAULT_MAX_DEPTH, help="how many hops of the test's reach"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown"
    )
    args = parser.parse_args(argv)
    return cmd_localize(
        Path(args.root),
        args.test,
        ref=args.ref,
        staged=args.staged,
        depth=args.depth,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
