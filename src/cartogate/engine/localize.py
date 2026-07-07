"""LOCALIZE (F-02) — rank the likely culprits behind a failing test.

The signal is the intersection of the graph and the change: of the symbols a failing test
*exercises* (reachable over EXTRACTED call/reference edges), which ones the change actually
*touched*. Those are the suspects, ranked nearest-to-the-test first.

Sound and advisory: it rests only on EXTRACTED structural edges (through the same exercise-edge
set FLAG uses) intersected with the deterministic diff→symbol mapping; it suggests where to look,
it never blocks. CFG/PDG slicing (F-03) will later refine this from symbol-level to statement-level.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cartogate.engine.cfg import build_cfg
from cartogate.engine.langspec import function_at
from cartogate.engine.pdg import build_pdg, observable_output_nodes
from cartogate.engine.traversal import EXERCISE_EDGE_TYPES
from cartogate.schema.enums import Confidence, NodeKind
from cartogate.store.base import Direction, FileRegion, StoreInterface

#: How far to follow what a test exercises (transitively) when hunting for the culprit. 4 covers a
#: typical stack (test -> fixture -> helper -> impl) without unbounded traversal on large graphs;
#: callers override via ``depth``. A culprit deeper than this is silently out of range — so the
#: report carries ``depth_searched`` to make that bound visible.
DEFAULT_MAX_DEPTH = 4


@dataclass(frozen=True, slots=True)
class Suspect:
    """A changed symbol within the failing test's reach — a localization candidate."""

    qualified_name: str
    location: str  # path:line
    distance: int  # call-graph hops from the test (1 = directly exercised)
    changed_lines: tuple[int, ...] = ()  # CFG-reachable changed lines (statement-level refinement)
    output_relevant_lines: tuple[int, ...] = ()  # changed lines reaching an observable output (PDG)
    output_analyzed: bool = False  # whether the PDG output-slice ran (else the fields are unknown)


@dataclass(frozen=True, slots=True)
class LocalizeReport:
    """Ranked culprits for a failing test (nearest first), or why none were found."""

    found: bool
    test: str
    suspects: tuple[Suspect, ...] = ()
    reason: str = ""
    depth_searched: int = 0  # how many hops the reach was explored — the bound on the search

    def to_dict(self) -> dict[str, Any]:
        """Serialise the report as a JSON-ready dict."""
        return {
            "found": self.found,
            "test": self.test,
            "reason": self.reason,
            "depth_searched": self.depth_searched,
            "suspects": [
                {
                    "qualified_name": s.qualified_name,
                    "location": s.location,
                    "distance": s.distance,
                    "changed_lines": list(s.changed_lines),
                    "output_relevant_lines": list(s.output_relevant_lines),
                    "output_analyzed": s.output_analyzed,
                }
                for s in self.suspects
            ],
            "count": len(self.suspects),
        }

    def to_markdown(self) -> str:
        """Render the report as a Markdown advisory block for a tool response / PR comment."""
        title = f"## Cartogate localize: `{self.test}`\n"
        if not self.found:
            return f"{title}\n{self.reason}\n"
        if not self.suspects:
            return (
                f"{title}\nNo changed code within {self.depth_searched} hop(s) of this test — the "
                f"cause may be elsewhere (an untracked dependency, fixture, or the environment), "
                f"or deeper than {self.depth_searched} hops (raise `depth`).\n"
            )
        lines = []
        for i, s in enumerate(self.suspects, start=1):
            extra = (
                f" — changed line(s): {', '.join(map(str, s.changed_lines))}"
                if s.changed_lines
                else ""
            )
            # The PDG hint reads against the CFG-filtered changed lines, so it shows only once
            # refine_with_cfg has run (changed_lines populated) — the CLI runs them in that order.
            if s.output_analyzed and s.changed_lines:
                extra += (
                    f" → flow to output: {', '.join(map(str, s.output_relevant_lines))}"
                    if s.output_relevant_lines
                    else " → no observable effect found"
                )
            lines.append(f"{i}. `{s.qualified_name}` — {s.location} (distance {s.distance}){extra}")
        return (
            f"{title}\n{len(self.suspects)} changed symbol(s) in this test's reach — "
            f"likely culprits, nearest first:\n\n" + "\n".join(lines) + "\n"
        )


def _reachable_with_distance(
    store: StoreInterface, start_id: str, *, max_depth: int
) -> dict[str, int]:
    """BFS over EXTRACTED exercise edges from ``start_id``; map each reached node to its hop
    distance (the smallest number of hops). ``start_id`` itself is excluded."""
    distance: dict[str, int] = {}
    seen = {start_id}
    frontier = [start_id]
    for hop in range(1, max_depth + 1):
        nxt: list[str] = []
        for node_id in frontier:
            for edge in store.neighbors(
                node_id,
                edge_types=EXERCISE_EDGE_TYPES,
                direction=Direction.OUT,
                confidence=(Confidence.EXTRACTED,),
            ):
                if edge.dst not in seen:
                    seen.add(edge.dst)
                    distance[edge.dst] = hop
                    nxt.append(edge.dst)
        if not nxt:
            break
        frontier = nxt
    return distance


def localize(
    store: StoreInterface,
    failing_test: str,
    regions: list[FileRegion],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> LocalizeReport:
    """Rank the symbols a failing test exercises that the change (``regions``) touched — the likely
    culprits, nearest to the test first. Deterministic."""
    test_node = store.get_symbol(failing_test)
    if test_node is None:
        return LocalizeReport(False, failing_test, reason=f"test {failing_test!r} not in the graph")

    distance = _reachable_with_distance(store, test_node.id, max_depth=max_depth)
    changed_ids = set(store.changed_set(regions))

    suspects: list[Suspect] = []
    for node_id, hops in distance.items():
        if node_id not in changed_ids:
            continue
        node = store.get_node(node_id)
        if node is None or node.kind is not NodeKind.SYMBOL:
            continue
        suspects.append(
            Suspect(
                node.qualified_name,
                f"{node.location.path}:{node.location.start_line}",
                hops,
            )
        )
    # Nearest first, then qualified name for a stable, deterministic order.
    suspects.sort(key=lambda s: (s.distance, s.qualified_name))
    return LocalizeReport(True, failing_test, suspects=tuple(suspects), depth_searched=max_depth)


def refine_with_cfg(
    report: LocalizeReport,
    regions: list[FileRegion],
    read_source: Callable[[str], bytes | None],
) -> LocalizeReport:
    """Refine function-level suspects to statement level using each suspect function's CFG (F-03).

    ``read_source(path)`` returns the file's bytes (or ``None`` to skip). A suspect whose changed
    lines all fall in **unreachable** statements of its function is **dropped** — a dead-code change
    can't be the cause, and this is sound because the CFG never under-counts reachability. A kept
    suspect is annotated with its reachable changed lines. Python only; a non-Python or unparseable
    suspect is kept unchanged (conservative).
    """
    if not report.found:
        return report
    changed_by_file: dict[str, set[int]] = {}
    for region in regions:
        changed_by_file.setdefault(region.path, set()).update(
            range(region.start_line, region.end_line + 1)
        )

    refined: list[Suspect] = []
    for suspect in report.suspects:
        path, _, line_text = suspect.location.rpartition(":")
        changed = changed_by_file.get(path)
        source = read_source(path) if path.endswith(".py") and line_text.isdigit() else None
        if source is None or not changed:
            refined.append(suspect)  # not Python / unreadable / nothing changed here — keep as-is
            continue
        func = function_at(source, int(line_text))
        body = func.child_by_field_name("body") if func is not None else None
        if func is None or body is None:
            refined.append(suspect)
            continue
        span = set(range(func.start_point[0] + 1, func.end_point[0] + 2))
        changed_in_func = changed & span
        if not changed_in_func:
            refined.append(suspect)
            continue
        cfg = build_cfg(body)
        reachable_changed = sorted(changed_in_func & cfg.reachable_lines())
        covered = changed_in_func & cfg.statement_lines()
        # Drop ONLY when every changed line is an unreachable statement. The `not (changed_in_func -
        # covered)` guard keeps the suspect if any changed line is NOT a body statement (a signature
        # / default-arg / decorator change is behaviour-affecting and must never be dropped).
        if covered and not reachable_changed and not (changed_in_func - covered):
            continue  # the change is confined to dead code -> drop (sound precision)
        refined.append(
            Suspect(
                suspect.qualified_name,
                suspect.location,
                suspect.distance,
                tuple(reachable_changed),
            )
        )
    return LocalizeReport(
        report.found, report.test, tuple(refined), report.reason, report.depth_searched
    )


def refine_with_pdg(
    report: LocalizeReport,
    regions: list[FileRegion],
    read_source: Callable[[str], bytes | None],
) -> LocalizeReport:
    """Refine suspects with intraprocedural program slices (F-03, the PDG layer over
    :func:`refine_with_cfg`).

    For each Python suspect, build its PDG and compute the backward slice of the function's
    **observable outputs** (return/raise/yield, any call, any external write — over-approximated by
    :func:`observable_output_nodes`). A suspect's changed lines that fall in that slice are recorded
    as ``output_relevant_lines``: they actually flow to what the function returns or does. A suspect
    whose changed lines reach **no** observable output is flagged (likely a no-op change — e.g. a
    touched comment or a dead store) and sorted *below* its peers at the same distance.

    It **never drops** a suspect: the observable-output set is over-approximated (so an unmarked
    line is provably a no-op), but this classifier is advisory and must never hide a real culprit —
    hence annotate-and-reorder, not prune. Non-Python / unreadable / no-change-here suspects pass
    through unanalysed. It preserves ``changed_lines`` set by :func:`refine_with_cfg`.
    """
    if not report.found:
        return report
    changed_by_file: dict[str, set[int]] = {}
    for region in regions:
        changed_by_file.setdefault(region.path, set()).update(
            range(region.start_line, region.end_line + 1)
        )

    refined: list[Suspect] = []
    for suspect in report.suspects:
        path, _, line_text = suspect.location.rpartition(":")
        changed = changed_by_file.get(path)
        source = read_source(path) if path.endswith(".py") and line_text.isdigit() else None
        if source is None or not changed:
            refined.append(suspect)  # not Python / unreadable / nothing changed here — keep as-is
            continue
        func = function_at(source, int(line_text))
        body = func.child_by_field_name("body") if func is not None else None
        if func is None or body is None:
            refined.append(suspect)
            continue
        span = set(range(func.start_point[0] + 1, func.end_point[0] + 2))
        changed_in_func = changed & span
        if not changed_in_func:
            refined.append(suspect)
            continue
        pdg = build_pdg(func)
        seeds = observable_output_nodes(pdg)
        # No observable output at all -> nothing to refute against; treat every changed line as
        # relevant (never demote on absence of evidence).
        relevant = set(pdg.slice_lines(pdg.backward_slice(seeds))) if seeds else span
        output_relevant = tuple(sorted(changed_in_func & relevant))
        refined.append(
            Suspect(
                suspect.qualified_name,
                suspect.location,
                suspect.distance,
                suspect.changed_lines,
                output_relevant,
                output_analyzed=True,
            )
        )
    # Within a distance: analysed-with-output first; demote ONLY a suspect proven to reach no output
    def _rank(s: Suspect) -> tuple[int, bool, str]:
        return (s.distance, s.output_analyzed and not s.output_relevant_lines, s.qualified_name)

    refined.sort(key=_rank)
    return LocalizeReport(
        report.found, report.test, tuple(refined), report.reason, report.depth_searched
    )


# `function_at` now lives in engine.langspec (it is language-aware) and is imported above; it is
# re-exported here so the existing Python-only callers can keep importing it from this module.
__all__ = [
    "LocalizeReport",
    "Suspect",
    "function_at",
    "localize",
    "refine_with_cfg",
    "refine_with_pdg",
]
