"""Per-statement variable def/use for Go (F-03/F-08, PDG data-dependence input).

The Go counterpart of :mod:`cartogate.engine.def_use` — same :class:`DefUse` contract (weak/strong
split, over-approximating so a real dependence is never missed) over the tree-sitter-go grammar.
Strong defs: a clean rebind of a plain name (``:=`` short decl, a ``var``/``const`` spec, a plain
``x =`` / ``x +=`` / ``x++``, a ``range`` target, a parameter). Weak defs (gen, no kill): a write
through a selector (``a.b =``) or an index (``a[i] =``), and the receiver / a name passed to a call.

Residual under-approximation (documented, out of scope for v1): aliasing through pointers
(``p := &a; *p = 1``) and reflection. Go has no module-global write marker, but package-level vars
written through a selector/index are caught as weak defs; a plain-name write to a package var is the
analogue of the documented Python global residual.
"""

from __future__ import annotations

from tree_sitter import Node as TSNode

from cartogate.engine.def_use import DefUse

_COMMENTS = frozenset({"comment"})
GO_SCOPE_TYPES = frozenset({"function_declaration", "method_declaration", "func_literal"})
_DECLS = frozenset({"var_declaration", "const_declaration"})
_CALLS = frozenset({"call_expression"})


def _name(node: TSNode) -> str:
    """The identifier text of ``node``."""
    return (node.text or b"").decode("utf-8", "replace")


def _idents(node: TSNode) -> set[str]:
    """Root identifiers of an lvalue (``a`` of ``a.b``, the base of ``a[i]``)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            out.add(_name(cur))
        elif cur.type in ("selector_expression", "index_expression"):
            operand = cur.child_by_field_name("operand")
            if operand is not None:
                stack.append(operand)  # the field/index is not part of the lvalue root
        else:
            stack.extend(cur.named_children)
    return out


def _collect_reads(node: TSNode, uses: set[str], weak: set[str]) -> None:
    """Add every name *read* in ``node`` to ``uses``; record call-mutation weak defs."""
    kind = node.type
    if kind == "identifier":
        uses.add(_name(node))
        return
    if kind == "selector_expression":  # a.b -> read a, not the field
        operand = node.child_by_field_name("operand")
        if operand is not None:
            _collect_reads(operand, uses, weak)
        return
    if kind == "index_expression":  # a[i] -> read a and i
        for field in ("operand", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
        return
    if kind in _CALLS:
        fn = node.child_by_field_name("function")
        if fn is not None:
            _collect_reads(fn, uses, weak)
            if fn.type == "selector_expression":  # obj.M(...) — the callee may mutate obj
                operand = fn.child_by_field_name("operand")
                if operand is not None:
                    weak |= _idents(operand)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                _collect_reads(arg, uses, weak)
                if arg.type == "identifier":  # the callee may mutate a name passed in
                    weak.add(_name(arg))
                elif arg.type == "variadic_argument":
                    weak |= _idents(arg)
        return
    for child in node.named_children:
        _collect_reads(child, uses, weak)


def _reads(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Read an arbitrary expression (Go assignments are statements, not expressions — no walrus)."""
    _collect_reads(node, uses, weak)


def _target(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Classify an assignment/binding target: a plain name is a strong def; a selector/index is a
    weak def of its base; an ``expression_list`` (multi-assign) recurses."""
    kind = node.type
    if kind == "identifier":
        if _name(node) != "_":  # the blank identifier discards — not a binding
            defs.add(_name(node))
    elif kind == "expression_list":
        for child in node.named_children:
            _target(child, defs, weak, uses)
    elif kind == "selector_expression":  # a.b = … -> weak-def + read a
        operand = node.child_by_field_name("operand")
        if operand is not None:
            weak |= _idents(operand)
            _collect_reads(operand, uses, weak)
    elif kind == "index_expression":  # a[i] = … -> weak-def a; read a and i
        operand = node.child_by_field_name("operand")
        if operand is not None:
            weak |= _idents(operand)
        for field in ("operand", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
    else:
        _collect_reads(node, uses, weak)


def _assignment(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """``x = e`` / ``x += e`` / ``a, b = f()``: targets on the left, reads on the right."""
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    operator = node.child_by_field_name("operator")
    augmented = operator is not None and _name(operator) != "="  # +=, -=, |=, … -> def AND use
    if left is not None:
        _target(left, defs, weak, uses)
        if augmented:
            _collect_reads(left, uses, weak)
    if right is not None:
        _reads(right, defs, weak, uses)


def _short_var(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """``a, b := f()`` — the left names are strong defs; the right is reads."""
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is not None:
        _target(left, defs, weak, uses)
    if right is not None:
        _reads(right, defs, weak, uses)


def _declaration(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """A ``var``/``const`` declaration: each spec's name(s) are strong defs, the value is reads."""
    stack = list(node.named_children)
    while stack:
        spec = stack.pop()
        if spec.type in ("var_spec", "const_spec"):
            value = spec.child_by_field_name("value")
            for name in spec.children_by_field_name("name"):
                _target(name, defs, weak, uses)
            if value is not None:
                _reads(value, defs, weak, uses)
        elif spec.type == "var_spec_list":  # grouped `var ( … )`
            stack.extend(spec.named_children)


def _for_header(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """A ``for`` loop header (the body is separate CFG nodes): the clause's init/cond/update, the
    range target+iterable, or a bare condition."""
    for child in node.named_children:
        if child.type == "block":
            continue  # the body is separate CFG nodes
        if child.type == "for_clause":
            init = child.child_by_field_name("initializer")
            cond = child.child_by_field_name("condition")
            update = child.child_by_field_name("update")
            if init is not None:
                _collect_all(init, defs, weak, uses)
            if cond is not None:
                _reads(cond, defs, weak, uses)
            if update is not None:
                _collect_all(update, defs, weak, uses)
        elif child.type == "range_clause":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if left is not None:
                _target(left, defs, weak, uses)
            if right is not None:
                _reads(right, defs, weak, uses)
        else:  # bare condition: `for x < n { … }`
            _reads(child, defs, weak, uses)


def _collect_all(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Aggregate defs+uses over a whole statement subtree — folds opaque ``switch``/``select``/
    ``defer``/``go`` bodies (single CFG nodes) and any compounds nested inside them."""
    kind = node.type
    if kind in ("block", "statement_list"):
        for child in node.named_children:
            if child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    elif kind == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _reads(inner, defs, weak, uses)
    elif kind == "short_var_declaration":
        _short_var(node, defs, weak, uses)
    elif kind in _DECLS:
        _declaration(node, defs, weak, uses)
    elif kind == "assignment_statement":
        _assignment(node, defs, weak, uses)
    elif kind in ("inc_statement", "dec_statement"):  # x++ / x-- -> def AND use
        operand = node.named_children[0] if node.named_children else None
        if operand is not None:
            _target(operand, defs, weak, uses)
            _collect_reads(operand, uses, weak)
    elif kind in GO_SCOPE_TYPES:  # a nested func: opaque — bind its name, inner reads are uses
        name = node.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            defs.add(_name(name))
        captured: set[str] = set()
        _collect_reads(node, captured, weak)
        if name is not None and name.type == "identifier":
            captured.discard(_name(name))
        uses |= captured
    elif kind == "for_statement":
        _for_header(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "if_statement":
        init = node.child_by_field_name("initializer")
        cond = node.child_by_field_name("condition")
        if init is not None:
            _collect_all(init, defs, weak, uses)
        if cond is not None:
            _reads(cond, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("expression_switch_statement", "type_switch_statement"):
        init = node.child_by_field_name("initializer")
        value = node.child_by_field_name("value")
        if init is not None:
            _collect_all(init, defs, weak, uses)
        if value is not None:
            _reads(value, defs, weak, uses)
        for child in node.named_children:
            if child.type in ("expression_case", "type_case", "default_case"):
                _collect_all(child, defs, weak, uses)
    elif kind in ("expression_case", "type_case", "default_case", "communication_case"):
        value = node.child_by_field_name("value")
        if value is not None:
            _reads(value, defs, weak, uses)
        for child in node.named_children:
            if child is not value:
                _collect_all(child, defs, weak, uses)
    elif kind == "receive_statement":  # select `case v := <-ch` — left binds, right reads
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif kind == "labeled_statement":  # `Label: <stmt>` — fold the labelled stmt (skip the label)
        for child in node.named_children:
            if child.type != "label_name":
                _collect_all(child, defs, weak, uses)
    else:  # select/defer/go/return/…: reads + recurse nested bodies
        _reads(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)


def _recurse_blocks(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Recurse into the nested blocks/clauses of a compound the CFG leaves opaque."""
    for child in node.named_children:
        if child.type in ("block", "statement_list") or child.type.endswith("_case"):
            _collect_all(child, defs, weak, uses)


def def_use_for_node(ts: TSNode, kind: str) -> DefUse:
    """The def/use of one Go CFG statement node (mirrors ``def_use.def_use_for_node``)."""
    defs: set[str] = set()
    weak: set[str] = set()
    uses: set[str] = set()

    if kind == "condition":  # an if predicate expression (header — body is separate)
        _reads(ts, defs, weak, uses)
    elif ts.type == "for_statement":  # header only: clause / range / bare condition
        _for_header(ts, defs, weak, uses)
    else:  # every other statement — aggregate the whole subtree (folds opaque switch/select/defer)
        _collect_all(ts, defs, weak, uses)

    return DefUse(frozenset(uses), frozenset(defs), frozenset(weak))


def parameter_defs(func: TSNode) -> frozenset[str]:
    """The parameter (and receiver) names a Go function/method binds (strong defs at CFG ENTRY)."""
    out: set[str] = set()
    for field in ("receiver", "parameters"):
        plist = func.child_by_field_name(field)
        if plist is None:
            continue
        for param in plist.named_children:
            if param.type in ("parameter_declaration", "variadic_parameter_declaration"):
                for name in param.children_by_field_name("name"):
                    if name.type == "identifier" and _name(name) != "_":
                        out.add(_name(name))
    return frozenset(out)
