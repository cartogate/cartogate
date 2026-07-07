"""``cartogate hooks install|uninstall`` — keep the persistent snapshot fresh automatically (F-09).

The snapshot only helps if it reflects the current code, and asking a developer to remember to
re-run ``cartogate index`` is poor DX. This installs git hooks that refresh it at the natural change
points — **post-commit, post-merge, post-checkout** — so the graph updates whenever the working
tree's committed state changes.

The refresh is **non-blocking** (backgrounded) and **cheap** (``cartogate index`` reuses the F-36
incremental delta, so only changed files reparse), and it's guarded by ``command -v cartogate`` so a
missing binary never breaks a commit. Each hook carries a marker block, so install is idempotent and
uninstall removes only our lines — an existing hook's other contents are preserved.

(Rapid commits can spawn a few overlapping background refreshes; the snapshot write is atomic so
that's safe, just briefly wasteful — acceptable for now, since each refresh is incremental.)
"""

from __future__ import annotations

import re
import stat
import sys
from pathlib import Path

from cartogate.gitio import run_git

_HOOKS = ("post-commit", "post-merge", "post-checkout")
_BEGIN = "# >>> cartogate >>>"
_END = "# <<< cartogate <<<"
_BLOCK = (
    f"{_BEGIN}\n"
    "# Refresh the Cartogate code-graph snapshot in the background (non-blocking, incremental).\n"
    # `|| true` so the hook exits 0 even when cartogate isn't on PATH (git ignores post-* exit
    # codes, but a manual / `pre-commit`-framework run shouldn't see a spurious non-zero).
    "command -v cartogate >/dev/null 2>&1 && (cartogate index >/dev/null 2>&1 &) || true\n"
    f"{_END}\n"
)


def _hooks_dir(root: Path) -> Path | None:
    """The repo's git hooks directory, or ``None`` if ``root`` isn't a git repo."""
    out = run_git(["rev-parse", "--git-path", "hooks"], cwd=root, timeout=10)
    if out is None:
        return None
    rel = out.decode("utf-8", "replace").strip()
    hooks = Path(rel)
    return hooks if hooks.is_absolute() else (root / hooks)


def _strip_block(text: str) -> str:
    """Remove a previously-installed cartogate block (between the markers) from a hook's text.

    Only strips when BOTH markers are present, in order — a truncated/hand-mangled block (a lone
    ``_BEGIN``) is left untouched rather than dropping everything after it (never lose a user hook).
    """
    if _BEGIN not in text or _END not in text or text.index(_END) < text.index(_BEGIN):
        return text
    before, _, rest = text.partition(_BEGIN)
    _, _, after = rest.partition(_END)
    # one newline at the seam; strip surrounding blank lines so re-install doesn't accrete them.
    return (before.rstrip("\n") + "\n" + after.lstrip("\n")).strip("\n")


def install_hooks(root: Path) -> list[Path]:
    """Install (idempotently) the refresh hooks. Returns the hook paths written."""
    hooks_dir = _hooks_dir(root)
    if hooks_dir is None:
        raise RuntimeError(f"{root} is not a git repository — no hooks to install")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in _HOOKS:
        hook = hooks_dir / name
        existing = hook.read_text(encoding="utf-8") if hook.exists() else ""
        body = _strip_block(existing)  # drop any prior cartogate block (idempotent re-install)
        if not body.strip():
            new = "#!/bin/sh\n" + _BLOCK
        else:
            if not body.startswith("#!"):
                body = "#!/bin/sh\n" + body  # a hook must have a shebang; add one if missing
            new = body.rstrip("\n") + "\n" + _BLOCK  # our block goes after the existing hook body
        hook.write_text(new, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        written.append(hook)
    return written


def uninstall_hooks(root: Path) -> list[Path]:
    """Remove the cartogate block from each hook (leaving other contents). Returns touched paths."""
    hooks_dir = _hooks_dir(root)
    if hooks_dir is None:
        raise RuntimeError(f"{root} is not a git repository")
    touched: list[Path] = []
    for name in _HOOKS:
        hook = hooks_dir / name
        if not hook.exists():
            continue
        text = hook.read_text(encoding="utf-8")
        if _BEGIN not in text:
            continue
        stripped = _strip_block(text)
        # Delete the file if nothing but a shebang (any interpreter) remains — it was only ours.
        if not stripped.strip() or re.fullmatch(r"#![^\n]*", stripped.strip()):
            hook.unlink()  # the hook was only ours -> remove it entirely
        else:
            hook.write_text(stripped + "\n", encoding="utf-8")
        touched.append(hook)
    return touched


def cmd_hooks(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    root = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("-") else Path(".")
    root = root.resolve()
    try:
        if action == "install":
            for hook in install_hooks(root):
                print(f"cartogate: installed {hook}")
            print("cartogate: the snapshot will refresh on commit / merge / checkout")
            return 0
        if action == "uninstall":
            touched = uninstall_hooks(root)
            for hook in touched:
                print(f"cartogate: cleaned {hook}")
            if not touched:
                print("cartogate: no cartogate hooks were installed")
            return 0
    except RuntimeError as exc:
        print(f"cartogate: {exc}", file=sys.stderr)
        return 1
    print("usage: cartogate hooks {install|uninstall} [repo]", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    return cmd_hooks(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
