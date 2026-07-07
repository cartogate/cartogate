"""``cartogate slice`` — intraprocedural program slices (F-03).

``cartogate slice <file>:<line>`` prints the **backward slice** — the statements that affect the
value/behaviour at that line (over control + data dependence). ``--forward`` prints the forward
slice (what that line affects). ``--interprocedural`` follows calls across function boundaries
(backward only; indexes ``--root``). Py/JS/TS/Go/Java/C; advisory — reports, never fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cartogate.engine.langspec import SliceLang, function_at, lang_for_path
from cartogate.engine.pdg import build_pdg
from cartogate.engine.sdg import DEFAULT_DEPTH, interprocedural_backward_slice
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def _slice(source: bytes, line: int, *, forward: bool, lang: SliceLang) -> dict[str, Any] | None:
    """The slice at ``line`` as ``{lines, statements}``; ``None`` if no function/statement there."""
    func = function_at(source, line, lang)
    if func is None:
        return None
    pdg = build_pdg(func, lang)
    seed = pdg.seed_for_line(line)
    if seed is None:
        return None
    sliced = pdg.forward_slice([seed]) if forward else pdg.backward_slice([seed])
    return pdg.to_dict(sliced)


def cmd_slice(target: str, *, forward: bool = False, as_json: bool = False) -> int:
    path_text, _, line_text = target.rpartition(":")  # rpartition keeps a Windows drive intact
    if not path_text or not line_text.isdigit():
        print(f"cartogate slice: expected <file>:<line>, got {target!r}", file=sys.stderr)
        return 1
    path, line = Path(path_text), int(line_text)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # before any output; report/paths may be non-ASCII
    lang = lang_for_path(path)
    if lang is None:
        print(f"cartogate slice: unsupported language ({path.suffix or '?'})")
        return 0  # advisory: nothing to slice, not an error
    try:
        source = path.read_bytes()
    except OSError as exc:
        print(f"cartogate slice: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    result = _slice(source, line, forward=forward, lang=lang)
    if result is None:
        print(f"cartogate slice: no statement inside a function at {path}:{line}")
        return 0  # advisory: nothing to slice
    print(json.dumps(result, indent=2) if as_json else _markdown(target, line, forward, result))
    return 0


def cmd_slice_interproc(target: str, root: Path, *, depth: int, as_json: bool) -> int:
    """Backward interprocedural slice: index ``root``, then follow calls out from the seed line."""
    path_text, _, line_text = target.rpartition(":")
    if not path_text or not line_text.isdigit():
        print(f"cartogate slice: expected <file>:<line>, got {target!r}", file=sys.stderr)
        return 1
    line = int(line_text)
    root = root.resolve()
    target_path = Path(path_text)
    abs_path = (target_path if target_path.is_absolute() else root / target_path).resolve()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if lang_for_path(abs_path) is None:
        print(f"cartogate slice: unsupported language ({abs_path.suffix or '?'})")
        return 0
    try:
        rel = abs_path.relative_to(root).as_posix()
    except ValueError:
        print(f"cartogate slice: {path_text} is not under --root {root}", file=sys.stderr)
        return 1

    store = InMemoryStore()
    try:
        index_package(root, repo_id=root.name, store=store, base=root)
    except Exception as exc:  # CLI boundary: a clean message beats a traceback
        print(f"cartogate slice: failed to index {root}: {exc}", file=sys.stderr)
        return 1

    def _read(rel_path: str) -> bytes | None:
        try:
            return (root / rel_path).read_bytes()
        except OSError:
            return None

    result = interprocedural_backward_slice(store, _read, rel, line, depth=depth)
    if result is None:
        print(f"cartogate slice: no statement inside a function at {rel}:{line}")
        return 0
    print(json.dumps(result.to_dict(), indent=2) if as_json else result.to_markdown())
    return 0


def _markdown(target: str, line: int, forward: bool, result: dict[str, Any]) -> str:
    direction = "forward" if forward else "backward"
    relation = "affected by" if forward else "that affect"
    statements = result["statements"]
    head = (
        f"## Cartogate slice: `{target}` ({direction})\n\n"
        f"{len(statements)} statement(s) {relation} line {line}:\n"
    )
    body = "\n".join(f"- {s['line']}: `{s['code']}`" for s in statements)
    return head + "\n" + body + "\n"


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args and dispatch to ``cmd_slice``."""
    parser = argparse.ArgumentParser(
        prog="cartogate slice",
        description="Program slice: the statements that affect (or are affected by) a line.",
    )
    parser.add_argument("target", help="<file>:<line> (1-based) to slice from, e.g. src/mod.py:42")
    parser.add_argument(
        "--forward", action="store_true", help="forward slice (what this line affects)"
    )
    parser.add_argument(
        "--interprocedural",
        action="store_true",
        help="follow calls across functions (backward only; indexes --root)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help=f"interprocedural call-expansion depth (default {DEFAULT_DEPTH})",
    )
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repo root to index for --interprocedural"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown"
    )
    args = parser.parse_args(argv)
    if args.interprocedural:
        if args.forward:
            print(
                "cartogate slice: --forward is not supported with --interprocedural "
                "(interprocedural slicing is backward only)",
                file=sys.stderr,
            )
            return 1
        return cmd_slice_interproc(args.target, args.root, depth=args.depth, as_json=args.as_json)
    return cmd_slice(args.target, forward=args.forward, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
