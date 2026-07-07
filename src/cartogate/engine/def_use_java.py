"""Per-statement variable def/use for Java (F-03/F-08, PDG data-dependence input).

The Java counterpart of :mod:`cartogate.engine.def_use` — same :class:`DefUse` contract (weak/strong
split, over-approximating so a real dependence is never missed) over the tree-sitter-java grammar.
Strong defs: a clean rebind of a plain name (a ``local_variable_declaration`` declarator, a plain
``x =`` / ``x +=`` / ``x++``, an enhanced-for variable, a parameter). Weak defs (gen, no kill): a
write through a field access (``a.b =`` / ``this.f =``) or an array access (``a[i] =``), and the
receiver / a name passed to a method call or ``new``.

Residual under-approximation (documented, out of scope for v1): aliasing, reflection, and a
plain-name write to a *field* without an explicit ``this`` qualifier (an unqualified ``f = x`` where
``f`` is a field reads as a strong local def — the Java analogue of the Python global residual).
"""

from __future__ import annotations

from tree_sitter import Node as TSNode

from cartogate.engine.def_use import DefUse

_COMMENTS = frozenset({"comment", "line_comment", "block_comment"})
JAVA_SCOPE_TYPES = frozenset({"lambda_expression", "class_body", "class_declaration"})
_CALLS = frozenset({"method_invocation", "object_creation_expression"})
_NAME_LIKE = frozenset({"identifier", "this", "super"})


def _name(node: TSNode) -> str:
    """The identifier text of ``node`` (``this``/``super`` keep their keyword text)."""
    return (node.text or b"").decode("utf-8", "replace")


def _idents(node: TSNode) -> set[str]:
    """Root names of an lvalue (``a`` of ``a.b``, ``this`` of ``this.f``, the base of ``a[i]``)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in _NAME_LIKE:
            out.add(_name(cur))
        elif cur.type == "field_access":
            obj = cur.child_by_field_name("object")
            if obj is not None:
                stack.append(obj)  # the field name is not part of the lvalue root
        elif cur.type == "array_access":
            array = cur.child_by_field_name("array")
            if array is not None:
                stack.append(array)
        else:
            stack.extend(cur.named_children)
    return out


def _collect_reads(node: TSNode, uses: set[str], weak: set[str]) -> None:
    """Add every name *read* in ``node`` to ``uses``; record call-mutation weak defs. Does not
    descend into a nested scope (lambda / anonymous class) for defs — its reads still count."""
    kind = node.type
    if kind in _NAME_LIKE:
        uses.add(_name(node))
        return
    if kind == "field_access":  # a.b -> read a, not the field
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_reads(obj, uses, weak)
        return
    if kind == "array_access":  # a[i] -> read a and i
        for field in ("array", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
        return
    if kind in _CALLS:
        obj = node.child_by_field_name("object")  # method_invocation receiver
        if obj is not None:
            _collect_reads(obj, uses, weak)
            weak |= _idents(obj)  # obj.m(...) — the call may mutate obj
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                _collect_reads(arg, uses, weak)
                if arg.type == "identifier":  # the callee may mutate a name passed in
                    weak.add(_name(arg))
        # the `name` field is the method name (not a variable) and is intentionally not read; a
        # constructor's `type` is a class name, also not a variable.
        return
    for child in node.named_children:
        _collect_reads(child, uses, weak)


def _reads(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Read an arbitrary expression (Java assignment is an expression — fold nested-assign defs)."""
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
            elif left is not None and left.type in ("field_access", "array_access"):
                weak |= _idents(left)
        elif cur.type == "update_expression":
            for child in cur.named_children:
                if child.type == "identifier":
                    defs.add(_name(child))
        stack.extend(cur.named_children)


def _target(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Classify an assignment target: a plain name is a strong def; a field/array access is a weak
    def of its base."""
    kind = node.type
    if kind == "identifier":
        defs.add(_name(node))
    elif kind == "field_access":  # a.b = … / this.f = … -> weak-def + read the base
        obj = node.child_by_field_name("object")
        if obj is not None:
            weak |= _idents(obj)
            _collect_reads(obj, uses, weak)
    elif kind == "array_access":  # a[i] = … -> weak-def a; read a and i
        array = node.child_by_field_name("array")
        if array is not None:
            weak |= _idents(array)
        for field in ("array", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
    else:
        _collect_reads(node, uses, weak)


def _expr(inner: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """The def/use of an expression statement's inner expression (assignment/update/bare)."""
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
    elif kind == "update_expression":  # i++ / ++i / obj.f++ / a[i]++ -> def AND use of the operand
        for child in inner.named_children:
            _target(child, defs, weak, uses)  # identifier -> strong; field/array -> weak of base
            _collect_reads(child, uses, weak)
    else:  # a bare expression / method-call statement
        _reads(inner, defs, weak, uses)


def _declaration(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """A ``local_variable_declaration``: each declarator's name is a strong def, value is reads."""
    for decl in node.children_by_field_name("declarator"):
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is not None:
            _target(name, defs, weak, uses)
        if value is not None:
            _reads(value, defs, weak, uses)


def _collect_all(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Aggregate defs+uses over a whole statement subtree — folds opaque ``try``/``switch``/
    ``synchronized``/labelled bodies (single CFG nodes) and any compounds nested inside them."""
    kind = node.type
    if kind == "block":
        for child in node.named_children:
            if child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    elif kind == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _expr(inner, defs, weak, uses)
    elif kind == "local_variable_declaration":
        _declaration(node, defs, weak, uses)
    elif kind in JAVA_SCOPE_TYPES:  # a nested lambda/class: opaque — inner reads are uses, no defs
        captured: set[str] = set()
        _collect_reads(node, captured, weak)
        uses |= captured
    elif kind == "for_statement":  # C-style: init + condition + update, then the body
        for field in ("init", "condition", "update"):
            part = node.child_by_field_name(field)
            if part is None:
                continue
            if field == "init" and part.type == "local_variable_declaration":
                _declaration(part, defs, weak, uses)
            elif field == "condition":
                _reads(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "enhanced_for_statement":  # for (T name : value)
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is not None:
            _target(name, defs, weak, uses)
        if value is not None:
            _reads(value, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("while_statement", "do_statement", "if_statement"):
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("switch_expression", "switch_statement"):  # opaque — read condition, fold body
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        body = node.child_by_field_name("body")
        if body is not None:
            _collect_all(body, defs, weak, uses)
    elif kind in ("switch_block", "switch_block_statement_group", "switch_rule"):
        for child in node.named_children:
            if child.type == "switch_label":  # `case CONST:` — the label expression is read
                _reads(child, defs, weak, uses)
            elif child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)  # case-body statements (defs + uses)
    elif kind == "catch_clause":  # catch (E e) { … } — the catch_formal_parameter child binds e
        for child in node.named_children:
            if child.type == "catch_formal_parameter":
                name = child.child_by_field_name("name")
                if name is not None:
                    _target(name, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "labeled_statement":  # `Label: <stmt>` — fold the labelled statement
        for child in node.named_children:
            if child.type != "identifier":
                _collect_all(child, defs, weak, uses)
    else:  # try/switch/synchronized/return/throw/…: reads + recurse nested bodies
        _reads(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)


_NESTED_CONTAINERS = frozenset(
    {"switch_block", "switch_block_statement_group", "switch_rule"}
)


def _recurse_blocks(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Recurse into the nested blocks/clauses of a compound the CFG leaves opaque."""
    for child in node.named_children:
        if (
            child.type == "block"
            or child.type.endswith("_clause")
            or child.type in _NESTED_CONTAINERS
        ):
            _collect_all(child, defs, weak, uses)


def def_use_for_node(ts: TSNode, kind: str) -> DefUse:
    """The def/use of one Java CFG statement node (mirrors ``def_use.def_use_for_node``)."""
    defs: set[str] = set()
    weak: set[str] = set()
    uses: set[str] = set()

    if kind == "condition":  # an if/while predicate expression (header — body is separate)
        _reads(ts, defs, weak, uses)
    elif ts.type == "for_statement":  # header only: init + condition + update
        for field in ("init", "condition", "update"):
            part = ts.child_by_field_name(field)
            if part is None:
                continue
            if field == "init" and part.type == "local_variable_declaration":
                _declaration(part, defs, weak, uses)
            elif field == "condition":
                _reads(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
    elif ts.type == "enhanced_for_statement":  # header only: variable + iterable
        name = ts.child_by_field_name("name")
        value = ts.child_by_field_name("value")
        if name is not None:
            _target(name, defs, weak, uses)
        if value is not None:
            _reads(value, defs, weak, uses)
    elif ts.type in ("while_statement", "do_statement"):  # header only: condition
        cond = ts.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
    else:  # every other statement — aggregate the whole subtree (folds opaque try/switch/sync)
        _collect_all(ts, defs, weak, uses)

    return DefUse(frozenset(uses), frozenset(defs), frozenset(weak))


def parameter_defs(func: TSNode) -> frozenset[str]:
    """The parameter names a Java method/constructor/lambda binds (strong defs at the CFG ENTRY)."""
    params = func.child_by_field_name("parameters")
    if params is None:
        return frozenset()
    if params.type == "identifier":  # a lambda's single bare param: ``x -> …``
        return frozenset({_name(params)})
    out: set[str] = set()
    for param in params.named_children:  # formal_parameters | inferred_parameters
        if param.type == "identifier":  # lambda inferred params: ``(a, b) -> …``
            out.add(_name(param))
        elif param.type == "formal_parameter":
            name = param.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                out.add(_name(name))
        elif param.type == "spread_parameter":  # varargs: ... declarator{name}
            for decl in param.named_children:
                if decl.type == "variable_declarator":
                    name = decl.child_by_field_name("name")
                    if name is not None:
                        out.add(_name(name))
    return frozenset(out)
