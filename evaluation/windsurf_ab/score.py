"""Oracle for the Windsurf A/B run's primary metric — did a trial introduce a duplicate?

Indexes a (post-task) tree and reports any duplicate top-level function/class signatures — the
same deterministic check the git pre-commit gate uses (``cartogate.surfaces.find_duplicate_
signatures``). This removes human judgement from the headline metric: a *reuse* task counts as a
miss iff this exits non-zero.

Usage:
    python -m evaluation.windsurf_ab.score [TREE]   # default: the bundled taskpack/

Exit 0 = no duplicate (clean); exit 1 = a duplicate signature was introduced (printed).
"""

from __future__ import annotations

import sys
from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_duplicate_signatures

DEFAULT_TREE = Path(__file__).parent / "taskpack"


def duplicates(tree: Path) -> dict[tuple[object, str], list[object]]:
    store = InMemoryStore()
    result = index_package(tree, repo_id=tree.name, store=store, resolve=False)
    return find_duplicate_signatures(list(result.nodes))


def main(argv: list[str]) -> int:
    tree = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_TREE
    dups = duplicates(tree)
    if not dups:
        print(f"clean: no duplicate top-level signatures in {tree}")
        return 0
    print(f"DUPLICATE introduced in {tree}:")
    for (language, signature), members in sorted(dups.items(), key=lambda kv: str(kv[0])):
        names = ", ".join(sorted(getattr(m, "qualified_name", str(m)) for m in members))
        print(f"  [{getattr(language, 'value', language)}] {signature}  ->  {names}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
