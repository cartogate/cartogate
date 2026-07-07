"""Per-statement variable def/use for C and C++ (F-03/F-08, PDG data-dependence input).

The C/C++ counterpart of :mod:`cartogate.engine.def_use` — same :class:`DefUse` contract (the
weak/strong split, over-approximating so a real dependence is never missed) over the tree-sitter-cpp
grammar (a C superset that parses both). Strong defs: a clean rebind of a plain name (a
``declaration`` declarator, a plain ``x =`` / ``x +=`` / ``x++``, a range-for variable, a ``catch``
binding, or a parameter). Weak defs (gen, no kill): a write through a field (``a.b =`` / ``p->n =``)
or a subscript (``a[i] =``), and the receiver / a name passed to a call or ``new``.

Residual under-approximation (documented, out of scope for v1): pointer/alias writes (``*p = x``
mutates ``*p``, tracked only as a read of ``p``), reflection-like macros, and a plain-name write to
a file/global scope variable (the C analogue of the Python global residual).
"""

from __future__ import annotations

from tree_sitter import Node as TSNode

from cartogate.engine.def_use import DefUse

_COMMENTS = frozenset({"comment"})
C_SCOPE_TYPES = frozenset({"lambda_expression", "function_definition"})
_CALLS = frozenset({"call_expression", "new_expression"})
_DECLARATOR_WRAPPERS = frozenset(
    {
        "init_declarator",
        "pointer_declarator",
        "array_declarator",
        "reference_declarator",
        "parenthesized_declarator",
    }
)


def _name(node: TSNode) -> str:
    """The identifier text of ``node``."""
    return (node.text or b"").decode("utf-8", "replace")


def _idents(node: TSNode) -> set[str]:
    """Root identifiers of an lvalue (``a`` of ``a.b``/``a->b``, the base of ``a[i]``)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            out.add(_name(cur))
        elif cur.type in ("field_expression", "subscript_expression"):
            arg = cur.child_by_field_name("argument")
            if arg is not None:
                stack.append(arg)  # the field/index is not part of the lvalue root
        elif cur.type == "pointer_expression":  # *p / &p -> the root is p
            arg = cur.child_by_field_name("argument")
            if arg is not None:
                stack.append(arg)
        else:
            stack.extend(cur.named_children)
    return out


def _collect_reads(node: TSNode, uses: set[str], weak: set[str]) -> None:
    """Add every name *read* in ``node`` to ``uses``; record call-mutation weak defs."""
    kind = node.type
    if kind == "identifier":
        uses.add(_name(node))
        return
    if kind == "field_expression":  # a.b / p->b -> read a/p, not the field
        arg = node.child_by_field_name("argument")
        if arg is not None:
            _collect_reads(arg, uses, weak)
        return
    if kind == "subscript_expression":  # a[i] -> read a and the indices
        for field in ("argument", "indices", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
        return
    if kind in _CALLS:
        fn = node.child_by_field_name("function")
        if fn is not None:
            _collect_reads(fn, uses, weak)
            if fn.type == "field_expression":  # obj.m(...) / p->m(...) — may mutate the receiver
                arg = fn.child_by_field_name("argument")
                if arg is not None:
                    weak |= _idents(arg)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                _collect_reads(arg, uses, weak)
                if arg.type == "identifier":  # the callee may mutate a name passed in
                    weak.add(_name(arg))
                elif arg.type == "pointer_expression":  # &x / *p passed in -> may be mutated
                    weak |= _idents(arg)
        new_decl = node.child_by_field_name("declarator")  # `new T[n]` -> read the size n
        if new_decl is not None:
            _collect_reads(new_decl, uses, weak)
        return
    for child in node.named_children:
        _collect_reads(child, uses, weak)


def _reads(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Read an arbitrary expression: uses + call-mutation weak defs + nested-assignment defs."""
    _collect_reads(node, uses, weak)
    _assign_defs(node, defs, weak)


def _assign_defs(node: TSNode, defs: set[str], weak: set[str]) -> None:
    """Defs from assignment/update *expressions* nested in a read (``x = (y = 1)``)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "assignment_expression":
            left = cur.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                defs.add(_name(left))
            elif left is not None and left.type in ("field_expression", "subscript_expression"):
                weak |= _idents(left)
        elif cur.type == "update_expression":
            arg = cur.child_by_field_name("argument")
            if arg is not None and arg.type == "identifier":
                defs.add(_name(arg))
        stack.extend(cur.named_children)


def _target(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Classify an assignment target: a plain name is a strong def; a field/subscript is a weak def
    of its base; a pointer deref (``*p =``) is an aliasing write (read of ``p`` only — residual)."""
    kind = node.type
    if kind == "identifier":
        defs.add(_name(node))
    elif kind == "field_expression":  # a.b = … / p->b = … -> weak-def + read the base
        arg = node.child_by_field_name("argument")
        if arg is not None:
            weak |= _idents(arg)
            _collect_reads(arg, uses, weak)
    elif kind == "subscript_expression":  # a[i] = … -> weak-def a; read a and i
        arg = node.child_by_field_name("argument")
        if arg is not None:
            weak |= _idents(arg)
        for field in ("argument", "indices", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
    else:  # pointer deref / parenthesized / other -> read it (aliasing write is a residual)
        _collect_reads(node, uses, weak)


def _declared_name(declarator: TSNode) -> str | None:
    """Descend a declarator chain (init/pointer/array/reference/parenthesized) to its identifier.
    ``reference_declarator`` has no ``declarator`` field — its inner declarator is a plain child —
    so fall back to the first named child when the field is absent."""
    cur: TSNode | None = declarator
    depth = 0
    while cur is not None and depth < 32:
        depth += 1
        if cur.type == "identifier":
            return _name(cur)
        if cur.type in _DECLARATOR_WRAPPERS:
            nxt = cur.child_by_field_name("declarator")
            if nxt is None:  # reference_declarator (& / &&): inner declarator is a plain child
                nxt = cur.named_children[0] if cur.named_children else None
            cur = nxt
        else:
            return None
    return None


def _declaration(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """A ``declaration``: each declarator binds a name (strong def); init values + array sizes are
    reads (the whole declarator is read — the name re-counted as a use is harmless over-approx)."""
    for decl in node.children_by_field_name("declarator"):
        name = _declared_name(decl)
        if name is not None:
            defs.add(name)
        _reads(decl, defs, weak, uses)  # init value, array size, etc. (name as a use is harmless)


def _expr(inner: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """The def/use of an expression statement's inner expression."""
    kind = inner.type
    if kind == "assignment_expression":
        left = inner.child_by_field_name("left")
        right = inner.child_by_field_name("right")
        operator = inner.child_by_field_name("operator")
        augmented = operator is not None and _name(operator) != "="  # +=, -=, … -> def AND use
        if left is not None:
            _target(left, defs, weak, uses)
            if augmented:
                _collect_reads(left, uses, weak)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif kind == "update_expression":  # i++ / ++i / a[i]++ / p->f++ -> def AND use of the operand
        arg = inner.child_by_field_name("argument")
        if arg is not None:
            _target(arg, defs, weak, uses)
            _collect_reads(arg, uses, weak)
    else:  # a bare expression / call statement
        _reads(inner, defs, weak, uses)


def _catch_param(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """The binding of a ``catch (T e)`` parameter (a strong def of e)."""
    params = node.child_by_field_name("parameters")
    if params is None:
        return
    for param in params.named_children:
        if param.type == "parameter_declaration":
            name = _declared_name(param.child_by_field_name("declarator") or param)
            if name is not None:
                defs.add(name)


def _collect_all(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Aggregate defs+uses over a whole statement subtree — folds opaque ``switch``/``try``/labelled
    bodies (single CFG nodes) and any compounds nested inside them."""
    kind = node.type
    if kind == "compound_statement":
        for child in node.named_children:
            if child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    elif kind == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _expr(inner, defs, weak, uses)
    elif kind == "declaration":
        _declaration(node, defs, weak, uses)
    elif kind in C_SCOPE_TYPES:  # a nested lambda/function: opaque — inner reads are uses, no defs
        captured: set[str] = set()
        _collect_reads(node, captured, weak)
        uses |= captured
    elif kind == "for_statement":  # C-style: initializer + condition + update, then the body
        for field in ("initializer", "condition", "update"):
            part = node.child_by_field_name(field)
            if part is None:
                continue
            if field == "initializer" and part.type == "declaration":
                _declaration(part, defs, weak, uses)
            elif field == "condition":
                _reads(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "for_range_loop":  # C++ for (T v : items)
        declarator = node.child_by_field_name("declarator")
        right = node.child_by_field_name("right")
        if declarator is not None:
            name = _declared_name(declarator)
            if name is not None:
                defs.add(name)
            else:
                _target(declarator, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("while_statement", "do_statement", "if_statement"):
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "switch_statement":  # opaque — read condition, fold the body (cases)
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        body = node.child_by_field_name("body")
        if body is not None:
            _collect_all(body, defs, weak, uses)
    elif kind == "case_statement":  # case <value>: … — value is read, the body statements recurse
        value = node.child_by_field_name("value")
        if value is not None:
            _reads(value, defs, weak, uses)
        for child in node.named_children:
            if child is not value and child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    elif kind == "catch_clause":  # catch (T e) { … } — bind e, fold the body
        _catch_param(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "labeled_statement":  # `Label: <stmt>` — fold the labelled statement
        for child in node.named_children:
            if child.type not in ("statement_identifier",) and child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    else:  # try/return/throw/goto/…: reads + recurse nested bodies
        _reads(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)


def _recurse_blocks(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Recurse into the nested blocks/clauses of a compound the CFG leaves opaque."""
    for child in node.named_children:
        if (
            child.type == "compound_statement"
            or child.type.endswith("_clause")
            or child.type == "case_statement"
        ):
            _collect_all(child, defs, weak, uses)


def def_use_for_node(ts: TSNode, kind: str) -> DefUse:
    """The def/use of one C/C++ CFG statement node (mirrors ``def_use.def_use_for_node``)."""
    defs: set[str] = set()
    weak: set[str] = set()
    uses: set[str] = set()

    if kind == "condition":  # an if/while predicate (a condition_clause wrapper — read through it)
        _reads(ts, defs, weak, uses)
    elif ts.type == "for_statement":  # header only: initializer + condition + update
        for field in ("initializer", "condition", "update"):
            part = ts.child_by_field_name(field)
            if part is None:
                continue
            if field == "initializer" and part.type == "declaration":
                _declaration(part, defs, weak, uses)
            elif field == "condition":
                _reads(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
    elif ts.type == "for_range_loop":  # header only: variable + iterable
        declarator = ts.child_by_field_name("declarator")
        right = ts.child_by_field_name("right")
        if declarator is not None:
            name = _declared_name(declarator)
            if name is not None:
                defs.add(name)
            else:
                _target(declarator, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif ts.type in ("while_statement", "do_statement"):  # header only: condition
        cond = ts.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
    else:  # every other statement — aggregate the whole subtree (folds opaque switch/try)
        _collect_all(ts, defs, weak, uses)

    return DefUse(frozenset(uses), frozenset(defs), frozenset(weak))


def parameter_defs(func: TSNode) -> frozenset[str]:
    """The parameter names a C/C++ function binds (strong defs at the CFG ENTRY)."""
    declarator = func.child_by_field_name("declarator")
    # peel wrappers to the (abstract) function declarator. A lambda's params live under
    # `abstract_function_declarator`; a named function's under `function_declarator`.
    while declarator is not None and declarator.type not in (
        "function_declarator",
        "abstract_function_declarator",
    ):
        declarator = declarator.child_by_field_name("declarator")
    if declarator is None:
        return frozenset()
    params = declarator.child_by_field_name("parameters")
    if params is None:
        return frozenset()
    out: set[str] = set()
    for param in params.named_children:
        if param.type == "parameter_declaration":
            name = _declared_name(param.child_by_field_name("declarator") or param)
            if name is not None:
                out.add(name)
    return frozenset(out)
