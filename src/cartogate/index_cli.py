"""``cartogate index`` — build/refresh the resolved graph snapshot (F-09).

Pre-warm or refresh a repo's snapshot without a running server: run it by hand, in CI, or from a git
hook (``cartogate hooks install``), and the next ``cartogate-mcp`` / ``cartogate daemon start
--resolve`` cold start loads it (seconds) instead of a full index. Writes
``<repo>/.cartogate/graph.json.gz``.

It reuses the daemon's :meth:`GitLazyRefresh.prime`, so a *second* run is incremental: it loads the
existing snapshot and re-extracts only the files that changed since (F-36), rather than rebuilding
the whole graph. That's what makes it cheap to run on every commit.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from cartogate.daemon.refresh import GitLazyRefresh
from cartogate.store.persist import graph_path


def cmd_index(root: Path, *, repo_id: str | None = None) -> int:
    """Build or incrementally refresh ``root``'s resolved snapshot, and persist it. Exit code.

    ``repo_id`` defaults to ``CARTOGATE_REPO_ID`` then the directory name — the same resolution the
    daemon/MCP use, so the snapshot's id matches (a mismatched id is rejected on load and rebuilt).
    """
    root = root.resolve()
    repo_id = repo_id or os.environ.get("CARTOGATE_REPO_ID") or root.name
    # prime() = load the snapshot + apply the content-hash delta (F-36), or full-build the first
    # time; it persists the result either way, so a repeat run is cheap: only changed files reparse
    # (F-36 incremental — the whole point of running it on every commit).
    refresh = GitLazyRefresh(root, repo_id=repo_id, resolve=True, index_docs=True)
    started = time.monotonic()
    store = refresh.prime()
    elapsed = time.monotonic() - started
    mode = refresh.last_refresh.mode if refresh.last_refresh is not None else "?"
    print(
        f"cartogate: {mode} index — {len(store.visible_node_ids())} nodes in {elapsed:.1f}s"
    )
    print(f"cartogate: snapshot at {graph_path(root)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args and not args[0].startswith("-") else Path(".")
    return cmd_index(root)


if __name__ == "__main__":
    raise SystemExit(main())
