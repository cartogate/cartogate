"""Section 3 gate — diff parsing → changed regions (risk R4, Windows-safe).

The engine turns a git diff into ``FileRegion``s (new-file line ranges) that the store
maps onto changed nodes. Parsing must be deterministic and path-normalized; the git
integration test exercises the real subprocess path on the host OS (Windows here),
including CRLF and forward-slash path handling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cartogate.engine.diff import git_diff_regions, parse_unified_diff
from cartogate.store.base import FileRegion

SAMPLE_DIFF = """\
diff --git a/pkg/auth.py b/pkg/auth.py
--- a/pkg/auth.py
+++ b/pkg/auth.py
@@ -5,1 +5,2 @@
-old
+new1
+new2
@@ -20 +21 @@
-x
+y
diff --git a/pkg/new.py b/pkg/new.py
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,3 @@
+a
+b
+c
"""


def test_parse_unified_diff_regions() -> None:
    regions = set(parse_unified_diff(SAMPLE_DIFF))
    assert regions == {
        FileRegion("pkg/auth.py", 5, 6),
        FileRegion("pkg/auth.py", 21, 21),
        FileRegion("pkg/new.py", 1, 3),
    }


def test_parse_ignores_pure_deletion_to_dev_null() -> None:
    deletion = (
        "diff --git a/gone.py b/gone.py\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-a\n-b\n-c\n"
    )
    # A file deleted entirely has no new-file region to attribute changes to.
    assert parse_unified_diff(deletion) == []


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_diff_regions_on_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    target = repo / "mod.py"
    target.write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    # Change line 6 (the body of b()).
    target.write_text("def a():\n    return 1\n\n\ndef b():\n    return 99\n", encoding="utf-8")

    regions = git_diff_regions(repo)
    assert any(r.path == "mod.py" and r.start_line <= 6 <= r.end_line for r in regions)
