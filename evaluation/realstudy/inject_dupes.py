"""Duplicate gate evaluation on real symbols (V4) — objective labels by construction.

No hand-labeling: we generate proposed signatures from real corpus symbols whose duplicate
status is objectively known, then score Cartogate's ``check_duplicate`` and a name-grep baseline
against those labels.

- **Exact** signature of a real top-level function → a true duplicate (must block).
- **Extra-param** variant of that function → NOT a duplicate (different signature; must not block).
- **A real method's** signature proposed as a top-level def → NOT a top-level duplicate
  (methods of different classes legitimately share shapes; must not block).

A name-grep baseline (``def <name>(`` exists anywhere) over-blocks the latter two, which is the
measured gap. We also report a specificity census of real signature collisions.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import NodeKind
from cartogate.schema.nodes import Node
from cartogate.schema.signature import normalize_signature

from .scoring import score


def _grep_defines(package_dir: Path, name: str) -> bool:
    pattern = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b")
    for path in sorted(package_dir.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(pattern.match(line) for line in text.splitlines()):
            return True
    return False


def _extra_param(signature: str) -> str:
    """Return a variant of ``signature`` with one extra parameter (a guaranteed non-duplicate)."""
    open_i = signature.find("(")
    close_i = signature.rfind(")")
    if open_i == -1 or close_i == -1 or close_i < open_i:
        return signature.rstrip(": ") + "(_rs_extra):"
    inner = signature[open_i + 1:close_i].strip()
    params = "_rs_extra" if not inner else f"{inner}, _rs_extra"
    return f"{signature[:open_i]}({params}){signature[close_i + 1:]}"


def evaluate(
    tools: CartogateTools, nodes: tuple[Node, ...], package_dir: Path, *, sample: int = 25
) -> dict[str, Any]:
    syms = [n for n in nodes if n.kind is NodeKind.SYMBOL and n.signature]
    top = sorted(
        (n for n in syms if n.is_top_level and "<" not in (n.signature or "")),
        key=lambda n: n.qualified_name,
    )
    methods = sorted(
        (n for n in syms if not n.is_top_level), key=lambda n: n.qualified_name
    )

    cases: list[dict[str, Any]] = []

    def add_case(kind: str, signature: str, name: str, is_dup: bool) -> None:
        gg = tools.check_duplicate(signature)["blocked"]
        grep = _grep_defines(package_dir, name)
        cases.append(
            {"kind": kind, "signature": signature, "is_duplicate": is_dup,
             "cartogate_blocked": gg, "grep_blocked": grep}
        )

    for n in top[:sample]:
        add_case("exact", n.signature or "", n.name, True)
        add_case("extra_param", _extra_param(n.signature or ""), n.name, False)
    for n in methods[:sample]:
        add_case("method_as_toplevel", n.signature or "", n.name, False)

    truth = {i for i, c in enumerate(cases) if c["is_duplicate"]}
    gg_block = {i for i, c in enumerate(cases) if c["cartogate_blocked"]}
    grep_block = {i for i, c in enumerate(cases) if c["grep_blocked"]}
    gg_score = score(gg_block, truth)
    grep_score = score(grep_block, truth)

    # Specificity census: real top-level signature collisions (gate's genuine flags) vs the
    # method collisions that top-level scoping correctly excludes.
    def census(group_nodes: list[Node]) -> tuple[int, int]:
        groups: dict[tuple[str, str], set[str]] = defaultdict(set)
        for n in group_nodes:
            key = (n.language.value, normalize_signature(n.signature or "", n.language))
            groups[key].add(n.qualified_name)
        collisions = {k: v for k, v in groups.items() if len(v) > 1}
        return len(collisions), sum(len(v) for v in collisions.values())

    tl_groups, tl_syms = census(top)
    m_groups, m_syms = census(methods)

    return {
        "cases": cases,
        "cartogate": gg_score.to_dict(),
        "grep_baseline": grep_score.to_dict(),
        "census": {
            "top_level_signature_collision_groups": tl_groups,
            "top_level_symbols_in_collisions": tl_syms,
            "method_collision_groups_excluded": m_groups,
            "method_symbols_excluded": m_syms,
            "top_level_total": len(top),
            "method_total": len(methods),
        },
        "sample": sample,
    }
