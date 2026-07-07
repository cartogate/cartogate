"""``cartogate viz <root>`` — index a tree and export its graph for viewing."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.viz.export import to_graphml, to_json
from cartogate.viz.html import to_html

_FORMATS = ("graphml", "html", "json")


def cmd_viz(
    root: Path,
    *,
    out_dir: Path,
    formats: list[str],
    max_nodes: int,
    repo_id: str | None = None,
) -> int:
    """Index ``root`` (full graph) and write the requested export formats to ``out_dir``."""
    root = root.resolve()
    store = InMemoryStore()
    try:
        result = index_package(root, repo_id=repo_id or root.name, store=store)
    except Exception as exc:  # CLI boundary: a clean message beats a traceback
        print(f"cartogate viz: failed to index {root}: {exc}", file=sys.stderr)
        return 1
    nodes, edges = result.nodes, result.edges

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "graphml" in formats:
        path = out_dir / "graph.graphml"
        path.write_text(to_graphml(nodes, edges), encoding="utf-8")
        written.append(path)
    if "json" in formats:
        path = out_dir / "graph.json"
        path.write_text(to_json(nodes, edges), encoding="utf-8")
        written.append(path)
    if "html" in formats:
        path = out_dir / "graph.html"
        path.write_text(
            to_html(nodes, edges, title=root.name, max_nodes=max_nodes), encoding="utf-8"
        )
        written.append(path)

    print(
        f"cartogate viz: indexed {result.files_indexed} files -> "
        f"{len(nodes)} nodes, {len(edges)} edges"
    )
    for path in written:
        print(f"  wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cartogate viz",
        description="Export the code graph to GraphML / JSON / a self-contained interactive HTML.",
    )
    parser.add_argument("root", nargs="?", default=".", help="package/source root to index")
    parser.add_argument(
        "--out-dir", default=None,
        help="output directory (default: <root>/.cartogate/viz — gitignored repo-local state)",
    )
    parser.add_argument("--format", choices=[*_FORMATS, "all"], default="all")
    parser.add_argument("--max-nodes", type=int, default=1500, help="SVG node cap (HTML only)")
    ns = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    formats = list(_FORMATS) if ns.format == "all" else [ns.format]
    root = Path(ns.root)
    if ns.out_dir is not None:
        out_dir = Path(ns.out_dir)
    else:
        from cartogate.daemon.discovery import ensure_state_dir

        out_dir = ensure_state_dir(root.resolve()) / "viz"
    return cmd_viz(root, out_dir=out_dir, formats=formats, max_nodes=ns.max_nodes)


if __name__ == "__main__":
    raise SystemExit(main())
