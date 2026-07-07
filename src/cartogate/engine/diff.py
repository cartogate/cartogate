"""Diff → changed regions (spec §8.1 ``changed_set`` feeder, risk R4).

Parses a unified git diff into ``FileRegion``s describing the *new-file* line ranges that
changed; the store maps those regions onto the nodes they overlap. Run with ``-U0`` so the
hunk ranges are tight. Paths are normalized to forward slashes (git already emits them that
way) to match the POSIX unit paths the extractor stores — the Windows-safety requirement.
"""

from __future__ import annotations

import re
from pathlib import Path

from cartogate.gitio import run_git
from cartogate.store.base import FileRegion

#: Matches a unified-diff hunk header, capturing the new-file start line and optional count.
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NEW_FILE = re.compile(r"^\+\+\+ (.+)$")


def parse_unified_diff(diff_text: str) -> list[FileRegion]:
    """Parse a unified diff into the changed new-file regions (one per hunk)."""
    regions: list[FileRegion] = []
    current_path: str | None = None

    for line in diff_text.splitlines():
        new_file = _NEW_FILE.match(line)
        if new_file is not None:
            current_path = _normalize_target(new_file.group(1))
            continue
        hunk = _HUNK.match(line)
        if hunk is None or current_path is None:
            continue
        start = int(hunk.group(1))
        count = int(hunk.group(2)) if hunk.group(2) is not None else 1
        if count == 0:
            # Pure deletion: no new-file lines to attribute the change to.
            continue
        regions.append(FileRegion(current_path, start, start + count - 1))
    return regions


def git_diff_regions(
    repo_dir: Path,
    *,
    staged: bool = False,
    ref: str | None = None,
) -> list[FileRegion]:
    """Run ``git diff -U0`` in ``repo_dir`` and return the changed regions.

    Args:
        repo_dir: Repository working directory.
        staged: Diff the index against HEAD (``--cached``) — used by the pre-commit gate.
        ref: Optional commit/range to diff against instead of the working tree.
    """
    args = ["diff", "-U0", "--no-color"]
    if staged:
        args.append("--cached")
    if ref is not None:
        args.append(ref)
    # run_git hardens against the Windows pipe-inheritance hang (unstaged diff scans the worktree
    # like status/ls-files) and bounds it with a timeout. None means git failed (not-a-repo, bad
    # ref, git missing) OR timed out/hung — distinct from "no changes" (exit 0, empty output ->
    # empty bytes -> no regions). Raise so a caller never mistakes a broken invocation for a clean
    # tree, and a hung diff surfaces instead of blocking forever.
    out = run_git(args, cwd=repo_dir, timeout=30)
    if out is None:
        raise RuntimeError(
            f"git diff failed or timed out in {repo_dir} (not a repo, bad ref, or git hung)"
        )
    return parse_unified_diff(out.decode("utf-8", "replace"))


def _normalize_target(target: str) -> str | None:
    """Normalize a ``+++`` target path; return ``None`` for /dev/null (deletion)."""
    target = target.strip()
    if target == "/dev/null":
        return None
    # Strip the conventional ``b/`` (or ``a/``) diff prefix and normalize separators.
    if target.startswith(("a/", "b/")):
        target = target[2:]
    return target.replace("\\", "/")
