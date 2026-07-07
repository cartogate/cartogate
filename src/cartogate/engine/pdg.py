"""Program-dependence graph + program slices (F-03) — the intraprocedural debugging payoff.

Composes the CFG, control dependence (``postdom``) and **data** dependence (reaching definitions
over per-statement def/use) into one graph whose edges mean "src *affects* dst", then exposes
backward slices ("what statements affect this line") and forward slices ("what this line affects").

Lazy/in-memory per function, **advisory**, and out of the gate (R7): ``control_dep``/``data_dep``
never reach ``GATE_EDGE_TYPES``. Over-approximating throughout (it inherits def/use's weak/strong
split and the CFG's conservative opacity), so a real dependence is never missed — a slice may be a
superset, never a subset.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tree_sitter import Node as TSNode

from cartogate.engine.cfg import ControlFlowGraph, build_cfg
from cartogate.engine.def_use import DefUse
from cartogate.engine.langspec import PYTHON, SliceLang
from cartogate.engine.postdom import control_dependences


@dataclass(frozen=True, slots=True)
class PDGEdge:
    """``src`` *affects* ``dst``: a data def->use (carrying ``var``) or a control predicate->dep."""

    src: int
    dst: int
    dep: str  # "data" | "control"
    var: str = ""
    label: str = ""


def reaching_definitions(
    cfg: ControlFlowGraph, du: dict[int, DefUse], entry_defs: frozenset[str]
) -> dict[int, set[tuple[str, int]]]:
    """Classic forward reaching-definitions fixpoint over the CFG. A fact is ``(var, def_node)``.
    A strong def kills prior defs of its var; a weak def gens without killing. ENTRY gens the
    parameters. Returns IN[n] for each ENTRY-reachable node."""
    reachable = cfg.reachable_from_entry()
    nodes = sorted(reachable)
    preds: dict[int, list[int]] = {n: [] for n in nodes}
    for edge in cfg.edges:
        if edge.src in reachable and edge.dst in reachable:
            preds[edge.dst].append(edge.src)

    gen: dict[int, set[tuple[str, int]]] = {}
    kill_vars: dict[int, set[str]] = {}
    for node in nodes:
        if node == cfg.entry:
            gen[node] = {(name, cfg.entry) for name in entry_defs}
            kill_vars[node] = set()  # ENTRY has no predecessors -> nothing to kill
        elif node in du:
            info = du[node]
            gen[node] = {(name, node) for name in (info.defs | info.weak_defs)}
            kill_vars[node] = set(info.defs)  # only strong defs kill
        else:
            gen[node] = set()
            kill_vars[node] = set()

    in_sets: dict[int, set[tuple[str, int]]] = {n: set() for n in nodes}
    out_sets: dict[int, set[tuple[str, int]]] = {n: set(gen[n]) for n in nodes}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            incoming: set[tuple[str, int]] = set()
            for pred in preds[node]:
                incoming |= out_sets[pred]
            killed = {fact for fact in incoming if fact[0] in kill_vars[node]}
            new_out = gen[node] | (incoming - killed)
            if incoming != in_sets[node] or new_out != out_sets[node]:
                in_sets[node] = incoming
                out_sets[node] = new_out
                changed = True
    return in_sets


def data_dependences(
    cfg: ControlFlowGraph, du: dict[int, DefUse], entry_defs: frozenset[str]
) -> list[PDGEdge]:
    """A data edge ``def_node -> use_node`` for every use of a var whose definition reaches it."""
    in_sets = reaching_definitions(cfg, du, entry_defs)
    out: list[PDGEdge] = []
    for node in sorted(in_sets):
        info = du.get(node)
        if info is None:
            continue
        for var, def_node in in_sets[node]:
            if var in info.uses:
                out.append(PDGEdge(def_node, node, "data", var=var))
    return out


@dataclass
class ProgramDependenceGraph:
    """The PDG over a function's CFG: control_dep ∪ data_dep edges (``src`` affects ``dst``)."""

    cfg: ControlFlowGraph
    edges: tuple[PDGEdge, ...]
    du: dict[int, DefUse] = field(default_factory=dict)  # per-node def/use (for output seeds)
    _preds: dict[int, list[int]] = field(init=False, default_factory=dict)  # computed adjacency
    _succs: dict[int, list[int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        preds: dict[int, set[int]] = {}
        succs: dict[int, set[int]] = {}
        for edge in self.edges:
            preds.setdefault(edge.dst, set()).add(edge.src)
            succs.setdefault(edge.src, set()).add(edge.dst)
        self._preds = {n: sorted(s) for n, s in preds.items()}
        self._succs = {n: sorted(s) for n, s in succs.items()}

    def predecessors(self, node_id: int) -> list[int]:
        return self._preds.get(node_id, [])

    def successors(self, node_id: int) -> list[int]:
        return self._succs.get(node_id, [])

    def _closure(self, seeds: Iterable[int], forward: bool) -> set[int]:
        seen = set(seeds)
        frontier = list(seen)
        step = self.successors if forward else self.predecessors
        while frontier:
            nxt: list[int] = []
            for node in frontier:
                for neighbour in step(node):
                    if neighbour not in seen:
                        seen.add(neighbour)
                        nxt.append(neighbour)
            frontier = nxt
        return seen

    def backward_slice(self, seeds: Iterable[int]) -> set[int]:
        """Statements that (transitively) affect any seed — the seeds included."""
        return self._closure(seeds, forward=False)

    def forward_slice(self, seeds: Iterable[int]) -> set[int]:
        """Statements (transitively) affected by any seed — the seeds included."""
        return self._closure(seeds, forward=True)

    def seed_for_line(self, line: int) -> int | None:
        """The innermost real CFG node covering ``line`` (the most specific statement there)."""
        sentinels = {self.cfg.entry, self.cfg.exit}
        covering = [
            node_id
            for node_id, node in self.cfg.nodes.items()
            if node_id not in sentinels and node.start_line <= line <= node.end_line
        ]
        if not covering:
            return None
        return min(
            covering,
            key=lambda nid: (self.cfg.nodes[nid].end_line - self.cfg.nodes[nid].start_line, nid),
        )

    def slice_lines(self, node_ids: Iterable[int]) -> list[int]:
        """The sorted source lines covered by ``node_ids`` (sentinels excluded). A loop header spans
        its whole loop, so including it renders the body lines (a presentation over-include)."""
        sentinels = {self.cfg.entry, self.cfg.exit}
        lines: set[int] = set()
        for node_id in node_ids:
            if node_id in sentinels:
                continue
            node = self.cfg.nodes[node_id]
            lines.update(range(node.start_line, node.end_line + 1))
        return sorted(lines)

    def to_dict(self, node_ids: Iterable[int]) -> dict[str, Any]:
        """Render a slice (a set of node ids) as JSON-ready lines + their statements."""
        sentinels = {self.cfg.entry, self.cfg.exit}
        statements = [
            {
                "line": self.cfg.nodes[nid].start_line,
                "end_line": self.cfg.nodes[nid].end_line,
                "code": self.cfg.nodes[nid].text,
            }
            for nid in sorted(node_ids, key=lambda n: self.cfg.nodes[n].start_line)
            if nid not in sentinels
        ]
        return {"lines": self.slice_lines(node_ids), "statements": statements}


def build_pdg(func: TSNode, lang: SliceLang = PYTHON) -> ProgramDependenceGraph:
    """Build the PDG for a function-definition node (lazy, from source), using ``lang``'s CFG spec
    and def/use (default Python)."""
    cfg = build_cfg(func.child_by_field_name(lang.body_field), lang.cfg)  # None -> ENTRY->EXIT
    du = {nid: lang.def_use_for_node(ts, cfg.nodes[nid].kind) for nid, ts in cfg.ts_nodes.items()}
    entry_defs = lang.parameter_defs(func)

    edges: list[PDGEdge] = [
        PDGEdge(cd.predicate, cd.dependent, "control", label=cd.label)
        for cd in control_dependences(cfg)
    ]
    edges.extend(data_dependences(cfg, du, entry_defs))
    ordered = tuple(sorted(set(edges), key=lambda e: (e.src, e.dst, e.dep, e.var, e.label)))
    return ProgramDependenceGraph(cfg, ordered, du)


def _is_observable_stmt(ts: TSNode, lang: SliceLang) -> bool:
    """True if the statement returns/raises/yields or contains a call — it can affect what a caller
    observes. Does not descend into a nested scope (its body doesn't run here)."""
    if ts.type in lang.opaque_scopes:
        return False  # binding a nested scope is not itself an observable output
    observable = lang.output_types | lang.call_types
    stack = [ts]
    while stack:
        node = stack.pop()
        if node.type in observable:
            return True
        stack.extend(c for c in node.children if c.type not in lang.opaque_scopes)
    return False


def _global_nonlocal_names(ts_nodes: Iterable[TSNode], lang: SliceLang) -> frozenset[str]:
    """Names declared ``global``/``nonlocal`` anywhere in the function (not descending into nested
    scopes). A plain-name assignment to one of these is an *external* write — it mutates a module
    global or an enclosing local that a caller can observe."""
    names: set[str] = set()
    for root in ts_nodes:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in ("global_statement", "nonlocal_statement"):
                names.update(
                    c.text.decode()
                    for c in node.children
                    if c.type == "identifier" and c.text is not None
                )
            stack.extend(c for c in node.children if c.type not in lang.opaque_scopes)
    return frozenset(names)


def observable_output_nodes(
    pdg: ProgramDependenceGraph, lang: SliceLang = PYTHON
) -> set[int]:
    """CFG nodes whose effect is observable to a caller: return/raise/throw/yield, a call (possible
    side effect), or an external write — a weak def of an attribute/subscript/arg root, OR (Python)
    a plain-name assignment to a ``global``/``nonlocal`` variable.

    **Over-approximated** — the set is a superset of the true observable outputs, so the backward
    slice of it is a superset of everything that can affect observable behaviour, and a line
    *outside* that slice is (modulo the residuals below) a no-op. Flags changed lines that cannot
    affect what a function returns or does. Residual gaps (rare, documented): ``del`` of a
    global/attribute, aliasing-mediated writes, and (JS) a plain-name write to a closure variable
    (no ``global``/``nonlocal`` marker to detect it) — these stay out of scope."""
    global_names = (
        _global_nonlocal_names(pdg.cfg.ts_nodes.values(), lang)
        if lang.detect_global_writes
        else frozenset()
    )
    out: set[int] = set()
    for nid, ts in pdg.cfg.ts_nodes.items():
        info = pdg.du.get(nid)
        external_write = info is not None and (info.weak_defs or (info.defs & global_names))
        if external_write or _is_observable_stmt(ts, lang):
            out.add(nid)
    return out
