"""``cartogate cfg`` — intraprocedural control-flow analysis (F-03).

Surfaces the CFG's first consumer: **statement-level unreachable code** — statements no control path
can reach (after an unconditional ``return``/``raise``/``throw``/``break``, or after an ``if`` whose
every branch leaves). Conservative (unmodelled constructs fall through, so no false positives) and
advisory — it reports, it never fails the build. Runs across every language the slicing stack
supports (see ``engine/langspec.py``): Python, JavaScript/TypeScript, Go, Java, and C/C++.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cartogate.engine.cfg import build_cfg
from cartogate.engine.langspec import functions_in, lang_for_path
from cartogate.extract.pipeline import iter_source_files


def _scan(root: Path) -> list[dict[str, object]]:
    """Every unreachable statement under ``root`` — {path, line, end_line, code}, sorted. Uses the
    same git-aware file walk as the indexer (respects ``.gitignore`` inside a repo)."""
    findings: list[dict[str, object]] = []
    for path, _language in iter_source_files(root):  # git-aware (gitignore-respecting) walk
        lang = lang_for_path(path)
        if lang is None:  # an indexed language the slicer doesn't support yet (e.g. Rust/C#)
            continue
        try:
            source = path.read_bytes()
        except OSError:
            continue
        tree = lang.parser.parse(source)
        rel = path.relative_to(root).as_posix()
        for func in functions_in(tree.root_node, lang):
            body = func.child_by_field_name(lang.body_field)
            if body is None or body.type not in lang.cfg.block_types:
                continue  # bodyless decl / arrow with an expression body -> no unreachable code
            for dead in build_cfg(body, lang.cfg).unreachable_statements():
                findings.append(
                    {
                        "path": rel,
                        "line": dead.start_line,
                        "end_line": dead.end_line,
                        "code": dead.text,
                    }
                )
    findings.sort(key=lambda f: (f["path"], f["line"]))
    return findings


def cmd_cfg(root: Path, *, as_json: bool = False) -> int:
    root = root.resolve()
    findings = _scan(root)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if as_json:
        print(json.dumps({"unreachable": findings, "count": len(findings)}, indent=2))
        return 0
    if not findings:
        print("## Cartogate control-flow analysis\n\nNo unreachable statements found.")
        return 0
    lines = [f"- {f['path']}:{f['line']} — `{f['code']}`" for f in findings]
    print(
        f"## Cartogate control-flow analysis\n\n{len(findings)} unreachable statement(s) "
        f"(no control path reaches them):\n\n" + "\n".join(lines)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args and dispatch to ``cmd_cfg``."""
    parser = argparse.ArgumentParser(
        prog="cartogate cfg",
        description="Control-flow analysis: statement-level unreachable code (Py/JS/TS/Go/Java/C).",
    )
    parser.add_argument("root", nargs="?", default=".", help="source root to scan")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of Markdown"
    )
    args = parser.parse_args(argv)
    return cmd_cfg(Path(args.root), as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
