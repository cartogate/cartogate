"""Shared pruned/hardened file walking for extraction passes.

Extracted from ``pipeline.py`` so bolt-on passes (docs, routes) can use the
same walk without importing the pipeline — ``pipeline -> pass -> pipeline``
was a real import cycle (cartogate's own cycle advisory flagged it on the
routes-pass commit, 2026-07-20).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

#: Skip pathologically large files (generated/vendored blobs) — they are not worth
#: parsing on the index path and guard against runaway memory.
MAX_FILE_BYTES = 2_000_000

#: Directory names never worth indexing — virtualenvs, vendored deps, caches, VCS. The
#: *fallback* (non-git) exclusion set; inside a git repo, ``git_tracked_files`` (F-38) supersedes
#: this by respecting the real ``.gitignore``. Kept for non-git trees.
_EXCLUDED_DIRS = frozenset(
    {
        ".venv", "venv", "env", "site-packages", "__pycache__", ".git", "node_modules",
        "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
    }
)


def _indexable(path: Path) -> bool:
    """A real, not-too-large file (symlinks and oversized blobs are skipped on the index path)."""
    if path.is_symlink():
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def iter_files(root: Path, suffix: str, allow: list[Path] | None = None) -> Iterator[Path]:
    """Yield indexable files with ``suffix`` under ``root`` (skip symlinks, blobs, noise dirs).

    When ``allow`` (the git working set from :func:`~cartogate.extract.pipeline.git_tracked_files`)
    is given, yield only those files — never walking ignored/vendored trees at all. Otherwise fall
    back to a pruned manual walk that never enters the fixed noise-dir set (for non-git trees).
    NOTE: unlike the old ``rglob``, the fallback walk does NOT follow symlinked directories
    (cycle/vanished-target safety) — a repo that symlinks source trees into its root indexes fewer
    files than before; use git (the ``allow`` path) there."""
    if allow is not None:
        for path in allow:
            # The git layer already drops gitignored trees; `_EXCLUDED_DIRS` stays as a belt-and-
            # suspenders guard for the "untracked .venv that wasn't gitignored" case.
            if (
                path.suffix == suffix
                and not _EXCLUDED_DIRS.intersection(path.parts)
                and _indexable(path)
            ):
                yield path
        return
    # Manual pruned walk, NOT rglob: rglob physically descends into excluded trees before we can
    # filter its results — a pnpm node_modules store nests paths past Windows' limit and holds
    # dangling junctions, so the traversal itself raised FileNotFoundError and killed the whole
    # index. Pruning skips those trees entirely, and any unreadable/vanished/too-long directory is
    # skipped instead of fatal.
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _EXCLUDED_DIRS:
                                stack.append(Path(entry.path))
                        elif entry.name.endswith(suffix) and not entry.is_symlink():
                            found.append(Path(entry.path))
                    except OSError:
                        continue  # a single unstatable entry never kills the index
        except OSError:
            continue  # vanished/unreadable/path-too-long directory — skip it, keep indexing
    for path in sorted(found):
        if _indexable(path):
            yield path
