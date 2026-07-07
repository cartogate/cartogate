"""Post-dominators and control dependence over the intraprocedural CFG (F-03, PDG control edges).

A statement S is **control-dependent** on a predicate P when P's outcome decides whether S runs.
Computed the standard way (Ferrante–Ottenstein–Warren): post-dominators on the single-EXIT CFG,
then for each branch edge ``A --label--> B`` where B does not post-dominate A, every node on the
post-dominator-tree path from B up to (but excluding) the immediate post-dominator of A is
control-dependent on A.

Deterministic (sorted iteration; stable result order) and advisory — control_dep stays out of the
gate (R7). Restricted to nodes reachable from ENTRY; the CFG's single EXIT (every return/raise/loop
exit flows to it) makes the post-dominator fixpoint well-defined.
"""

from __future__ import annotations

from dataclasses import dataclass

from cartogate.engine.cfg import ControlFlowGraph


@dataclass(frozen=True, slots=True)
class ControlDep:
    """``dependent`` runs only when branch ``predicate`` takes outcome ``label``."""

    predicate: int
    dependent: int
    label: str


def post_dominators(cfg: ControlFlowGraph) -> dict[int, set[int]]:
    """Map each ENTRY-reachable node to its post-dominators (itself + every node on all paths to
    EXIT). Iterative dataflow on the reversed graph rooted at EXIT."""
    reachable = cfg.reachable_from_entry()
    nodes = sorted(reachable)
    succ: dict[int, list[int]] = {n: [] for n in nodes}
    for edge in cfg.edges:
        if edge.src in reachable and edge.dst in reachable:
            succ[edge.src].append(edge.dst)

    universe = set(nodes)
    pdom = {n: set(universe) for n in nodes}
    pdom[cfg.exit] = {cfg.exit}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node == cfg.exit:
                continue
            successors = succ[node]
            if successors:
                inter = set(universe)
                for s in successors:
                    inter &= pdom[s]
            else:
                # A non-EXIT node with no successors shouldn't occur (every CFG node reaches EXIT).
                # If one ever does, treat EXIT as its post-dominator so `ipdom` stays defined and
                # the control-dep walk over-approximates (extra edges) rather than ending early and
                # MISSING a dependence — the cardinal property.
                inter = {cfg.exit}
            updated = {node} | inter
            if updated != pdom[node]:
                pdom[node] = updated
                changed = True
    return pdom


def immediate_post_dominators(
    cfg: ControlFlowGraph, pdoms: dict[int, set[int]] | None = None
) -> dict[int, int]:
    """The immediate post-dominator of each node — its closest strict post-dominator (the one with
    the most post-dominators of its own). EXIT (root) has none."""
    pdoms = pdoms if pdoms is not None else post_dominators(cfg)
    ipdom: dict[int, int] = {}
    for node, doms in pdoms.items():
        candidates = doms - {node}
        if candidates:
            # closest = the candidate with the most post-dominators of its own. Strict post-doms
            # form a chain (post-dom tree path), so their set sizes are strictly ordered → no real
            # tie; -c is an unreachable, stable guard against any non-tree artifact.
            ipdom[node] = max(candidates, key=lambda c: (len(pdoms[c]), -c))
    return ipdom


def control_dependences(cfg: ControlFlowGraph) -> list[ControlDep]:
    """Direct control-dependence edges (deterministic, sorted). Only branch nodes (a successor that
    does not post-dominate them) produce edges. **Direct only** — transitivity is the caller's job
    (slicing follows the chain)."""
    reachable = cfg.reachable_from_entry()
    pdoms = post_dominators(cfg)
    ipdom = immediate_post_dominators(cfg, pdoms)

    out: set[ControlDep] = set()
    for edge in cfg.edges:
        a, b = edge.src, edge.dst
        if a not in reachable or b not in reachable:
            continue
        if b in pdoms[a]:
            continue  # B post-dominates A -> the edge guards nothing
        stop = ipdom.get(a)
        cur: int | None = b
        while cur is not None and cur != stop:
            if cur != cfg.exit:  # EXIT is a sentinel, never a "dependent" statement
                out.add(ControlDep(a, cur, edge.label))
            cur = ipdom.get(cur)
    return sorted(out, key=lambda c: (c.dependent, c.predicate, c.label))
