"""Intraprocedural control-flow graph (F-03) — statement-level CFG for a function body.

Language-neutral: the construction is driven by a :class:`CfgSpec` (the grammar's node types and
fields), so the same builder serves Python and the C-family (JS/TS, …). Defaults are Python's.

Built lazily from a function's tree-sitter body (not materialised in the symbol graph), this is the
substrate for finer debugging: statement-level LOCALIZE and, later, PDG slices. The first consumer
here is **intra-function unreachable-statement detection** — statement-level dead code (e.g. code
after an unconditional ``return``), complementing F-67's symbol-level check.

Construction is the standard reverse-threaded successor linking: each statement is a node; control
edges run to its successor(s). ``return``/``raise`` go to EXIT; ``break``/``continue`` to the
enclosing loop's exit/header. Constructs we don't model explicitly (``try``/``with``/``match``) are
treated as **opaque but fall-through** — an over-approximation of the edges, so a node is only ever
called unreachable when it provably is (no false "this is dead"). Everything is advisory and stays
out of the gate (R7: ``control_flow`` is excluded from ``GATE_EDGE_TYPES`` even once populated).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node as TSNode


@dataclass(frozen=True, slots=True)
class CfgSpec:
    """The per-language tree-sitter node types/fields the CFG builder needs. Lets the same
    reverse-threaded construction serve Python, the C-family (JS/TS, Java, C/C++) and Go — only the
    grammar's names differ. Defaults are Python's."""

    block_types: frozenset[str] = frozenset({"block"})
    returns: frozenset[str] = frozenset({"return_statement", "raise_statement"})
    loops: frozenset[str] = frozenset({"for_statement", "while_statement"})
    break_type: str = "break_statement"
    continue_type: str = "continue_statement"
    if_type: str = "if_statement"
    #: alternatives as a *repeated* field of elif/else clauses (Python) vs a single else (C-family).
    alternative_is_repeated: bool = True
    elif_types: frozenset[str] = frozenset({"elif_clause"})
    else_types: frozenset[str] = frozenset({"else_clause"})
    cond_field: str = "condition"
    consequence_field: str = "consequence"
    alternative_field: str = "alternative"
    body_field: str = "body"
    #: Python's else_clause holds its block under a ``body`` field; the C-family else_clause holds
    #: it as a plain child (``None`` -> take the first named child).
    else_body_field: str | None = "body"
    comment_types: frozenset[str] = frozenset({"comment"})
    #: Transparent statement-container types inside a block (Go wraps a block's statements in a
    #: ``statement_list``); flattened away so the block's real statements become CFG nodes.
    stmt_list_types: frozenset[str] = frozenset()
    #: An ``if``'s init-statement field (Go ``if x := f(); cond``) — emitted as a node before the
    #: condition. ``None`` where the language has no if-initializer (Python/JS).
    init_field: str | None = None


PY_CFG = CfgSpec()
#: JS/TS share the ``tsx`` grammar: ``statement_block`` bodies, ``throw`` not ``raise``, C-style and
#: for-in/do loops, and a single ``else_clause`` (which may wrap an ``if_statement`` for else-if).
JS_CFG = CfgSpec(
    block_types=frozenset({"statement_block"}),
    returns=frozenset({"return_statement", "throw_statement"}),
    loops=frozenset({"for_statement", "while_statement", "for_in_statement", "do_statement"}),
    alternative_is_repeated=False,
    elif_types=frozenset(),
    else_types=frozenset({"else_clause"}),
    else_body_field=None,
)
#: Go: ``block`` wraps statements in a ``statement_list``; one ``for`` (all loop forms); no raise
#: (``panic`` is a call); a single ``alternative`` (block or ``if`` for else-if); ``if`` init-stmt.
GO_CFG = CfgSpec(
    block_types=frozenset({"block"}),
    returns=frozenset({"return_statement"}),
    loops=frozenset({"for_statement"}),
    alternative_is_repeated=False,
    elif_types=frozenset(),
    else_types=frozenset(),
    else_body_field=None,
    stmt_list_types=frozenset({"statement_list"}),
    init_field="initializer",
)
#: Java: ``block`` bodies; ``throw`` not ``raise``; C-style + enhanced-for + while + do loops; a
#: single ``alternative`` (block/statement or ``if`` for else-if); no if-initializer.
JAVA_CFG = CfgSpec(
    block_types=frozenset({"block"}),
    returns=frozenset({"return_statement", "throw_statement"}),
    loops=frozenset(
        {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
    ),
    alternative_is_repeated=False,
    elif_types=frozenset(),
    else_types=frozenset(),
    else_body_field=None,
)
#: C/C++: ``compound_statement`` bodies; C-style + range-for + while + do loops; ``throw`` (C++); a
#: single ``else_clause`` (may wrap an ``if`` for else-if). The if/while condition is a
#: ``condition_clause`` wrapper — transparent for reads. Used for both C and C++ (cpp parses C).
C_CFG = CfgSpec(
    block_types=frozenset({"compound_statement"}),
    returns=frozenset({"return_statement", "throw_statement"}),
    loops=frozenset({"for_statement", "while_statement", "do_statement", "for_range_loop"}),
    alternative_is_repeated=False,
    elif_types=frozenset(),
    else_types=frozenset({"else_clause"}),
    else_body_field=None,
)


@dataclass(frozen=True, slots=True)
class CFGNode:
    """A control-flow node: a statement/predicate, or the ENTRY/EXIT sentinel."""

    id: int
    kind: str  # tree-sitter node type, or "entry"/"exit"
    start_line: int  # 1-based; 0 for sentinels
    end_line: int
    text: str  # first-line snippet, for display


@dataclass(frozen=True, slots=True)
class CFGEdge:
    src: int
    dst: int
    label: str = ""  # "", "true", "false", "loop", "break", "continue"


@dataclass(frozen=True, slots=True)
class _Loop:
    """The break/continue targets in scope while building a loop body."""

    brk: int  # node reached by `break` (the loop's normal exit)
    cont: int  # node reached by `continue` (the loop header/condition)


@dataclass
class ControlFlowGraph:
    """A function's statement-level CFG. ENTRY/EXIT are sentinels; the rest are statements."""

    nodes: dict[int, CFGNode]
    edges: tuple[CFGEdge, ...]
    entry: int
    exit: int
    #: The exact tree-sitter node each real CFG node was derived from (for def/use; PDG, F-03).
    #: Keyed by node id; ENTRY/EXIT sentinels are absent. Lazy/in-memory only (never serialised).
    ts_nodes: dict[int, TSNode] = field(default_factory=dict)

    def successors(self, node_id: int) -> list[int]:
        return [e.dst for e in self.edges if e.src == node_id]

    def reachable_from_entry(self) -> set[int]:
        """Node ids reachable from ENTRY over control-flow edges (a simple forward BFS)."""
        adjacency: dict[int, list[int]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.src, []).append(edge.dst)
        seen = {self.entry}
        frontier = [self.entry]
        while frontier:
            nxt: list[int] = []
            for node_id in frontier:
                for succ in adjacency.get(node_id, ()):
                    if succ not in seen:
                        seen.add(succ)
                        nxt.append(succ)
            frontier = nxt
        return seen

    def _real_node_lines(self, node_ids: set[int]) -> set[int]:
        """The 1-based source lines spanned by the given nodes, excluding ENTRY/EXIT sentinels."""
        lines: set[int] = set()
        for node_id in node_ids:
            if node_id in (self.entry, self.exit):
                continue
            node = self.nodes[node_id]
            lines.update(range(node.start_line, node.end_line + 1))
        return lines

    def reachable_lines(self) -> set[int]:
        """Source line numbers covered by a statement reachable from ENTRY."""
        return self._real_node_lines(self.reachable_from_entry())

    def statement_lines(self) -> set[int]:
        """Source line numbers covered by any statement node (reachable or not)."""
        return self._real_node_lines(set(self.nodes))

    def unreachable_statements(self) -> list[CFGNode]:
        """Real statements (not ENTRY/EXIT) unreachable from ENTRY — statement-level dead code.

        Sound by construction: unmodelled statements over-approximate their out-edges, so a node is
        reported only when no control path can reach it. Reported outermost-first, by line."""
        reachable = self.reachable_from_entry()
        sentinels = {self.entry, self.exit}
        dead = [
            node
            for node_id, node in self.nodes.items()
            if node_id not in reachable and node_id not in sentinels
        ]
        # Drop a dead node nested inside another dead node's line span (report the outermost).
        dead.sort(key=lambda n: (n.start_line, -n.end_line))
        out: list[CFGNode] = []
        for node in dead:
            nested = any(
                o.start_line <= node.start_line and node.end_line <= o.end_line for o in out
            )
            if not nested:
                out.append(node)
        return out


class _Builder:
    def __init__(self, spec: CfgSpec = PY_CFG) -> None:
        self.spec = spec
        self._nodes: dict[int, CFGNode] = {}
        self._ts: dict[int, TSNode] = {}  # node id -> the tree-sitter node it was built from
        self._edges: list[CFGEdge] = []
        self._counter = 0
        self.entry = self._sentinel("entry")
        self.exit = self._sentinel("exit")

    def _sentinel(self, kind: str) -> int:
        node_id = self._counter
        self._counter += 1
        self._nodes[node_id] = CFGNode(node_id, kind, 0, 0, kind.upper())
        return node_id

    def _node(self, ts: TSNode, *, kind: str | None = None) -> int:
        node_id = self._counter
        self._counter += 1
        text = (ts.text or b"").decode("utf-8", "replace").splitlines()
        self._nodes[node_id] = CFGNode(
            node_id,
            kind or ts.type,
            ts.start_point[0] + 1,
            ts.end_point[0] + 1,
            text[0].strip() if text else "",
        )
        self._ts[node_id] = ts  # retained for def/use extraction (PDG); sentinels have none
        return node_id

    def _edge(self, src: int, dst: int, label: str = "") -> None:
        self._edges.append(CFGEdge(src, dst, label))

    def _seq(self, stmts: list[TSNode], after: int, loop: _Loop | None) -> int:
        """Link ``stmts`` in order so the last falls through to ``after``; return the entry."""
        cur = after
        for stmt in reversed(stmts):
            cur = self._stmt(stmt, cur, loop)
        return cur

    def _block(self, block: TSNode | None, after: int, loop: _Loop | None) -> int:
        if block is None:
            return after
        return self._seq(self._statements_of(block), after, loop)

    def _statements_of(self, block: TSNode) -> list[TSNode]:
        """The real statements of a block, flattening a transparent statement-list wrapper (Go)."""
        out: list[TSNode] = []
        for child in block.named_children:
            if child.type in self.spec.comment_types:
                continue
            if child.type in self.spec.stmt_list_types:  # Go: block -> statement_list -> statements
                out.extend(c for c in child.named_children if c.type not in self.spec.comment_types)
            else:
                out.append(child)
        return out

    def _branch(self, node: TSNode | None, after: int, loop: _Loop | None) -> int:
        """A branch body that may be a block OR (C-family) a single statement."""
        if node is None:
            return after
        if node.type in self.spec.block_types:
            return self._block(node, after, loop)
        return self._stmt(node, after, loop)

    def _stmt(self, node: TSNode, after: int, loop: _Loop | None) -> int:
        kind = node.type
        spec = self.spec
        if kind in spec.returns:
            nid = self._node(node)
            self._edge(nid, self.exit)
            return nid
        # break/continue outside a loop is a SyntaxError; routing to EXIT is a safe approximation.
        if kind == spec.break_type:
            nid = self._node(node)
            self._edge(nid, loop.brk if loop is not None else self.exit, "break")
            return nid
        if kind == spec.continue_type:
            nid = self._node(node)
            self._edge(nid, loop.cont if loop is not None else self.exit, "continue")
            return nid
        if kind == spec.if_type:
            return self._if(node, after, loop)
        if kind in spec.loops:
            return self._loop(node, after, loop)
        # Simple or unmodelled-compound statement: falls through to its successor (conservative).
        nid = self._node(node)
        self._edge(nid, after)
        return nid

    def _if(self, node: TSNode, after: int, loop: _Loop | None) -> int:
        spec = self.spec
        cond = self._node(node.child_by_field_name(spec.cond_field) or node, kind="condition")
        then_entry = self._branch(node.child_by_field_name(spec.consequence_field), after, loop)
        self._edge(cond, then_entry, "true")
        if spec.alternative_is_repeated:  # Python: a repeated field of elif_clause/else_clause
            alts = list(node.children_by_field_name(spec.alternative_field))
            self._edge(cond, self._py_alternatives(alts, after, loop), "false")
        else:  # C-family: a single `alternative` (an else_clause, possibly wrapping an `else if`)
            alt = node.child_by_field_name(spec.alternative_field)
            self._edge(cond, self._c_else(alt, after, loop), "false")
        # Go `if x := f(); cond` — the init statement runs before the condition; make it the entry.
        init = node.child_by_field_name(spec.init_field) if spec.init_field else None
        if init is not None:
            init_node = self._node(init)
            self._edge(init_node, cond)
            return init_node
        return cond

    def _py_alternatives(self, alts: list[TSNode], after: int, loop: _Loop | None) -> int:
        """Python's elif/else chain (a repeated ``alternative`` field); return its entry."""
        if not alts:
            return after
        head, *rest = alts
        spec = self.spec
        if head.type in spec.elif_types:
            cond = self._node(head.child_by_field_name(spec.cond_field) or head, kind="condition")
            then_entry = self._branch(head.child_by_field_name(spec.consequence_field), after, loop)
            self._edge(cond, then_entry, "true")
            self._edge(cond, self._py_alternatives(rest, after, loop), "false")
            return cond
        return self._block(head.child_by_field_name(spec.else_body_field or "body"), after, loop)

    def _c_else(self, alt: TSNode | None, after: int, loop: _Loop | None) -> int:
        """C-family else: a single ``else_clause`` whose content is a block, a statement, or an
        ``if_statement`` (``else if``). Returns its entry (or ``after`` when there is no else)."""
        if alt is None:
            return after
        target = alt
        if alt.type in self.spec.else_types:  # unwrap the else_clause to its meaningful child
            named = [c for c in alt.named_children if c.type not in self.spec.comment_types]
            if not named:
                return after
            target = named[0]
        if target.type == self.spec.if_type:
            return self._if(target, after, loop)  # else if
        return self._branch(target, after, loop)

    def _loop(self, node: TSNode, after: int, loop: _Loop | None) -> int:
        # Header = the whole loop statement (its branch point). break -> after; continue/loop-back
        # -> header; the body's normal fall-through returns to the header (next iteration). Using
        # the full statement's span (not just the condition) lets a *dead* loop dedup to one finding
        # (its body nests in the header span). The loop's `else` clause is not modelled (omitting it
        # is conservative — false negatives only).
        header = self._node(node, kind=node.type)
        body_loop = _Loop(brk=after, cont=header)
        body_entry = self._branch(node.child_by_field_name(self.spec.body_field), header, body_loop)
        self._edge(header, body_entry, "loop")
        self._edge(header, after, "false")  # loop not entered / exhausted
        return header

    def finish(self) -> ControlFlowGraph:
        return ControlFlowGraph(
            nodes=dict(self._nodes),
            edges=tuple(self._edges),
            entry=self.entry,
            exit=self.exit,
            ts_nodes=dict(self._ts),
        )


def build_cfg(body_block: TSNode | None, spec: CfgSpec = PY_CFG) -> ControlFlowGraph:
    """Build the statement-level CFG for a function body (its block node), using ``spec`` for the
    language's node types (default Python). ``None`` (a bodyless function) yields ENTRY->EXIT."""
    builder = _Builder(spec)
    first = builder._block(body_block, builder.exit, loop=None)
    builder._edge(builder.entry, first)
    return builder.finish()
