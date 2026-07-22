"""``cartogate navmap`` — export a DRAFT navigation map seed from extracted routes.

Stage 2B PR 3 (nav freshness, spec §3a). The seed is deliberately a draft:
states require >=1 landmark to be schema-valid, and landmarks are genuinely
unextractable from route declarations — fabricating placeholders would violate
extraction honesty, so the draft inherits the schema's refusal until a human
fills them in. Transition suggestions (from links_to edges) go to a separate
``<out>.suggestions.json`` sidecar; the map schema's unknown-key refusal stays
intact. No browser, no [nav] extra required — this is the Cartogate side.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.schema.enums import EdgeType, NodeKind
from cartogate.store import InMemoryStore


def _state_id(pattern: str) -> str:
    """Deterministic state id for a url pattern: ``/users/:userId`` → ``users.userId``."""
    stripped = pattern.strip("/")
    if not stripped:
        return "root"
    return ".".join(seg.lstrip(":") for seg in stripped.split("/"))


def main(argv: list[str] | None = None) -> int:
    """CLI entry: index ``root``, emit the draft map + suggestions sidecar."""
    parser = argparse.ArgumentParser(
        prog="cartogate navmap",
        description=(
            "Export a DRAFT navigation map seeded from extracted routes. The draft "
            "does not validate until you fill in each state's landmarks — that is "
            "deliberate: landmarks are what PROVE a state, and cartogate will not "
            "invent them."
        ),
    )
    parser.add_argument("root", nargs="?", default=".", help="package/source root")
    parser.add_argument(
        "--out", default="navmap.draft.json", help="draft output path"
    )
    parser.add_argument("--app", default=None, help="app name (default: root dir name)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    root = Path(args.root).resolve()
    store = InMemoryStore()
    result = index_package(
        root, repo_id=root.name, store=store, base=root, index_docs=False
    )

    routes = sorted(
        (n for n in result.nodes if n.kind is NodeKind.ROUTE),
        key=lambda n: n.qualified_name,
    )
    if not routes:
        print(
            f"error: no route declarations extracted under {root} "
            "(supported: Next.js app/ + pages/ trees, React Router and "
            "Vue Router literals)",
            file=sys.stderr,
        )
        return 1

    # Deterministic ids with collision disambiguation: "/a/b" and "/a.b" both
    # naively map to "a.b" (inspector Medium) — the draft must never carry
    # duplicate ids, and colliding source patterns are reported by name so the
    # human can rename meaningfully.
    used_ids: dict[str, str] = {}  # id -> first pattern
    id_by_pattern: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []  # (id, first pattern, this pattern)
    for n in routes:
        base = _state_id(n.qualified_name)
        candidate = base
        counter = 2
        while candidate in used_ids:
            collisions.append((base, used_ids[base], n.qualified_name))
            candidate = f"{base}~{counter}"
            counter += 1
        used_ids[candidate] = n.qualified_name
        id_by_pattern[n.qualified_name] = candidate

    states = [
        {
            "id": id_by_pattern[n.qualified_name],
            "url": n.qualified_name,
            "landmarks": [],  # REQUIRED, unextractable: fill in >=1 per state
            "affordances": [],
        }
        for n in routes
    ]

    id_by_node = {n.id: id_by_pattern[n.qualified_name] for n in routes}
    suggestions = sorted(
        {
            (id_by_node[e.src], id_by_node[e.dst])
            for e in result.edges
            if e.type is EdgeType.LINKS_TO
            and e.src in id_by_node
            and e.dst in id_by_node
        }
    )

    draft = {
        "version": 1,
        "app": args.app or root.name,
        "states": states,
        "transitions": [],
        "flows": [],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # CLI boundary: no tracebacks
    out_path.write_text(
        json.dumps(draft, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if suggestions:
        sidecar_path = out_path.with_suffix(".suggestions.json")
        sidecar = {
            "comment": (
                "links_to-derived transition candidates. Copy into the map as "
                "transitions once each has a real affordance to click."
            ),
            "suggested_transitions": [
                {"from": src, "to": dst} for src, dst in suggestions
            ],
        }
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    for base, first, second in collisions:
        print(
            f"note: state id collision on {base!r} — {first!r} and {second!r} "
            "map to the same id; the later pattern got a '~n' suffix. Rename "
            "the ids meaningfully in the draft.",
            file=sys.stderr,
        )
    print(
        f"wrote {out_path} — DRAFT: {len(states)} state(s) need landmarks "
        "(>=1 each) before the map validates. Next: add a landmark to each "
        f"state, then `cartogate nav crawl --map {out_path}` to verify them "
        "live and propose more. `cartogate nav check` needs a flow — author "
        "one under \"flows\" once the states hold up."
        + (
            f"\nwrote {out_path.with_suffix('.suggestions.json')} — "
            f"{len(suggestions)} suggested transition(s)"
            if suggestions
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
