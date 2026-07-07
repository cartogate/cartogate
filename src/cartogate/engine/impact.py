"""PR-time impact summary (F-68) — one advisory report from a change's symbols.

Composes the three existing advisory views, at the review/CI moment, over the symbols a change
touches:

- **affected** — who depends on them (reverse reachability: the blast radius),
- **tests** — which tests exercise them (FLAG ``suggest_tests``),
- **docs** — which docs explicitly reference them and may now be stale (FLAG ``doc_drift``).

Zero-config and advisory (a summary, not a gate). It packages capability Cartogate already has at a
new moment rather than computing anything new — so it inherits the same EXTRACTED-only soundness as
the underlying queries and never blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cartogate.engine.flag import DocReport, FlagEngine, FlagReport
from cartogate.engine.traversal import REFERENCE_EDGE_TYPES, GatingTraversal
from cartogate.schema.enums import NodeKind
from cartogate.store.base import FileRegion, StoreInterface


@dataclass(frozen=True, slots=True)
class AffectedSymbol:
    """A symbol that references (depends on) a changed symbol — part of the blast radius."""

    qualified_name: str
    unit: str
    location: str  # path:line


@dataclass(frozen=True, slots=True)
class ImpactSummary:
    """The composed PR-time view: changed symbols and their affected code / tests / docs."""

    changed: tuple[str, ...]
    affected: tuple[AffectedSymbol, ...]
    tests: FlagReport
    docs: DocReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": list(self.changed),
            "affected": [
                {"qualified_name": a.qualified_name, "unit": a.unit, "location": a.location}
                for a in self.affected
            ],
            "tests": self.tests.to_dict()["tests"],
            "docs": self.docs.to_dict()["docs"],
            "counts": {
                "changed": len(self.changed),
                "affected": len(self.affected),
                "tests": len(self.tests.tests),
                "docs": len(self.docs.docs),
            },
        }

    def to_markdown(self) -> str:
        """A compact report for a PR comment / CI log."""
        if not self.changed:
            return "## Cartogate impact summary\n\nNo indexed symbols changed."
        head = (
            f"## Cartogate impact summary\n\n"
            f"**{len(self.changed)} changed** -> {len(self.affected)} affected, "
            f"{len(self.tests.tests)} test(s) to run, {len(self.docs.docs)} doc(s) to review.\n"
        )
        out = [head, _md_section("Changed symbols", [f"`{q}`" for q in self.changed])]
        out.append(
            _md_section(
                "Affected (callers / users)",
                [f"`{a.qualified_name}` — {a.location}" for a in self.affected],
                empty="None — no in-repo code depends on the changed symbols.",
            )
        )
        out.append(
            _md_section(
                "Tests to run",
                [
                    f"`{t.qualified_name}` ({t.unit}) — exercises {', '.join(t.exercises)}"
                    for t in self.tests.tests
                ],
                empty="None found — the changed symbols have no exercising tests.",
            )
        )
        out.append(
            _md_section(
                "Docs to review",
                [f"{d.path} — mentions {', '.join(d.mentions)}" for d in self.docs.docs],
                empty="None — no docs reference the changed symbols.",
            )
        )
        return "\n".join(out)


def _md_section(title: str, items: list[str], *, empty: str | None = None) -> str:
    if not items:
        return f"### {title}\n\n{empty}\n" if empty else ""
    return f"### {title}\n\n" + "\n".join(f"- {item}" for item in items) + "\n"


def changed_symbol_qnames(store: StoreInterface, regions: list[FileRegion]) -> list[str]:
    """Qualified names of the SYMBOL nodes overlapping ``regions`` (a diff's changed lines)."""
    return sorted(
        {
            node.qualified_name
            for node_id in store.changed_set(regions)
            if (node := store.get_node(node_id)) is not None and node.kind is NodeKind.SYMBOL
        }
    )


def build_impact_summary(
    store: StoreInterface, changed_qnames: list[str], *, depth: int = 1
) -> ImpactSummary:
    """Compose the affected-code / tests / docs views for ``changed_qnames`` (deterministic)."""
    traversal = GatingTraversal(store)
    flag = FlagEngine(store)

    changed_nodes = []
    seen: set[str] = set()
    for qname in sorted(set(changed_qnames)):
        node = store.get_symbol(qname)
        if node is not None and node.id not in seen:
            seen.add(node.id)
            changed_nodes.append(node)
    changed = tuple(node.qualified_name for node in changed_nodes)
    changed_ids = {node.id for node in changed_nodes}

    affected: dict[str, AffectedSymbol] = {}
    for node in changed_nodes:
        for caller in traversal.callers(node.id, depth=depth, edge_types=REFERENCE_EDGE_TYPES):
            if caller.id in changed_ids:
                continue  # a changed symbol referencing another isn't *additional* fallout
            affected[caller.qualified_name] = AffectedSymbol(
                caller.qualified_name,
                caller.unit,
                f"{caller.location.path}:{caller.location.start_line}",
            )

    return ImpactSummary(
        changed=changed,
        affected=tuple(affected[key] for key in sorted(affected)),
        tests=flag.tests_for_symbols(list(changed), depth=depth),
        docs=flag.docs_for_symbols(list(changed), depth=depth),
    )
