"""Per-statement variable def/use for JavaScript/TypeScript (F-03/F-08, PDG data-dependence input).

The JS/TS counterpart of :mod:`cartogate.engine.def_use` — same contract (:class:`DefUse` with the
weak/strong split, over-approximating toward inclusion so a real dependence is never missed) over
the ``tsx`` grammar. Strong defs: a clean rebind of a plain name (``let``/``const``/``var`` decl, a
plain ``x = …`` / ``x += …`` / ``x++``, a ``for (const x of …)`` target, a ``catch (e)`` binding, a
parameter, a destructuring binding). Weak defs (gen, no kill): a write through ``a.b``/``a[i]``, the
receiver / a name passed to a call or ``new``.

Residual under-approximation (documented, out of scope for v1): a plain-name write to a *closure*
variable (JS has no ``global``/``nonlocal`` marker to detect it from the statement alone), aliasing,
and dynamic (``eval``/``with``) constructs.
"""

from __future__ import annotations

from tree_sitter import Node as TSNode

from cartogate.engine.def_use import DefUse

_COMMENTS = frozenset({"comment"})
_DECLS = frozenset({"lexical_declaration", "variable_declaration"})
JS_SCOPE_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "generator_function",
        "arrow_function",
        "method_definition",
        "class_declaration",
        "class",
    }
)
_CALLS = frozenset({"call_expression", "new_expression"})


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
        elif cur.type in ("member_expression", "subscript_expression"):
            obj = cur.child_by_field_name("object")
            if obj is not None:
                stack.append(obj)  # the property/index is not part of the lvalue root
        else:
            stack.extend(cur.named_children)
    return out


def _collect_reads(node: TSNode, uses: set[str], weak: set[str]) -> None:
    """Add every name *read* in ``node`` to ``uses``; record call-mutation weak defs."""
    kind = node.type
    if kind == "identifier":
        uses.add(_name(node))
        return
    if kind == "shorthand_property_identifier":  # the `x` in an object literal `{ x }`
        uses.add(_name(node))
        return
    if kind == "member_expression":  # a.b -> read a, not the property
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_reads(obj, uses, weak)
        return
    if kind == "subscript_expression":  # a[i] -> read a and i
        for field in ("object", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
        return
    if kind in _CALLS:
        fn = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        if fn is not None:
            _collect_reads(fn, uses, weak)
            if fn.type == "member_expression":  # obj.m(...) — the callee may mutate obj
                obj = fn.child_by_field_name("object")
                if obj is not None:
                    weak |= _idents(obj)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                _collect_reads(arg, uses, weak)
                if arg.type == "identifier":  # the callee may mutate a name passed in
                    weak.add(_name(arg))
                elif arg.type == "spread_element":
                    weak |= _idents(arg)
        return
    for child in node.named_children:
        _collect_reads(child, uses, weak)


def _assign_defs(node: TSNode, defs: set[str], weak: set[str]) -> None:
    """Defs from assignment/update *expressions* nested in a read (JS allows ``x = (y = 1)``)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "assignment_expression":
            left = cur.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                defs.add(_name(left))
            elif left is not None and left.type in ("member_expression", "subscript_expression"):
                obj = left.child_by_field_name("object")
                if obj is not None:
                    weak |= _idents(obj)
        elif cur.type in ("update_expression", "augmented_assignment_expression"):
            arg = cur.child_by_field_name("argument") or cur.child_by_field_name("left")
            if arg is not None and arg.type == "identifier":
                defs.add(_name(arg))
        stack.extend(cur.named_children)


def _reads(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Read an arbitrary expression: uses + call-mutation weak defs + any nested-assignment def."""
    _collect_reads(node, uses, weak)
    _assign_defs(node, defs, weak)


def _target(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Classify an assignment/binding target (mirrors the Python ``_target`` over JS patterns)."""
    kind = node.type
    if kind in ("identifier", "shorthand_property_identifier_pattern"):
        defs.add(_name(node))
    elif kind in ("array_pattern", "object_pattern", "rest_pattern"):
        for child in node.named_children:
            _target(child, defs, weak, uses)
    elif kind == "pair_pattern":  # { key: target }
        value = node.child_by_field_name("value")
        if value is not None:
            _target(value, defs, weak, uses)
    elif kind in ("assignment_pattern", "object_assignment_pattern"):  # target = default
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif kind == "member_expression":  # a.b = … -> weak-def + read a
        obj = node.child_by_field_name("object")
        if obj is not None:
            weak |= _idents(obj)
            _collect_reads(obj, uses, weak)
    elif kind == "subscript_expression":  # a[i] = … -> weak-def a; read a and i
        obj = node.child_by_field_name("object")
        if obj is not None:
            weak |= _idents(obj)
        for field in ("object", "index"):
            part = node.child_by_field_name(field)
            if part is not None:
                _collect_reads(part, uses, weak)
    else:
        _collect_reads(node, uses, weak)


def _expr(inner: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Def/use of an expression statement's inner expr (assignment/augmented/update/bare)."""
    kind = inner.type
    if kind == "assignment_expression":
        left = inner.child_by_field_name("left")
        right = inner.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif kind == "augmented_assignment_expression":  # x += e -> x is def AND use
        left = inner.child_by_field_name("left")
        right = inner.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
            _collect_reads(left, uses, weak)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif kind == "update_expression":  # i++ / --x -> def AND use of the argument
        arg = inner.child_by_field_name("argument")
        if arg is not None:
            _target(arg, defs, weak, uses)
            _collect_reads(arg, uses, weak)
    else:  # a bare expression / call statement
        _reads(inner, defs, weak, uses)


def _declaration(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """A ``let``/``const``/``var`` decl: each declarator's name is a target, its value is reads."""
    for decl in node.named_children:
        if decl.type != "variable_declarator":
            continue
        name = decl.child_by_field_name("name")
        value = decl.child_by_field_name("value")
        if name is not None:
            _target(name, defs, weak, uses)
        if value is not None:
            _reads(value, defs, weak, uses)


def _collect_all(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Aggregate defs+uses over a whole statement subtree — folds opaque ``try``/``switch`` bodies
    and any compounds nested inside them (mirrors the Python ``_collect_all``)."""
    kind = node.type
    if kind == "statement_block":
        for child in node.named_children:
            if child.type not in _COMMENTS:
                _collect_all(child, defs, weak, uses)
    elif kind == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _expr(inner, defs, weak, uses)
    elif kind in _DECLS:
        _declaration(node, defs, weak, uses)
    elif kind in JS_SCOPE_TYPES:  # nested function/class: opaque — bind its name; inner reads=uses
        name = node.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            defs.add(_name(name))
        captured: set[str] = set()
        _collect_reads(node, captured, weak)
        if name is not None and name.type == "identifier":
            captured.discard(_name(name))
        uses |= captured
    elif kind == "for_statement":  # C-style: init + condition + increment, then the body
        for field in ("initializer", "condition", "increment"):
            part = node.child_by_field_name(field)
            if part is None:
                continue
            if field == "condition":
                _reads(part, defs, weak, uses)
            elif part.type in _DECLS:
                _declaration(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "for_in_statement":  # for (x of/in y)
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("while_statement", "do_statement", "if_statement", "else_clause"):
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "catch_clause":  # catch (e) { … } — e is a strong binding
        param = node.child_by_field_name("parameter")
        if param is not None:
            _target(param, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "switch_case":  # case <value>: … — value is read, bodies recurse
        value = node.child_by_field_name("value")
        if value is not None:
            _reads(value, defs, weak, uses)
        for child in node.named_children:
            if child is not value:
                _collect_all(child, defs, weak, uses)
    elif kind == "switch_default":  # default: … — body statements are direct children; recurse
        for child in node.named_children:
            _collect_all(child, defs, weak, uses)
    else:  # try/switch subject/return/throw/…: reads + recurse nested bodies
        _reads(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)


def _recurse_blocks(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Recurse into the nested blocks/clauses of a compound that the CFG leaves opaque."""
    for child in node.named_children:
        if (
            child.type == "statement_block"
            or child.type.endswith("_clause")
            or child.type in ("switch_body", "switch_case", "switch_default")
        ):
            _collect_all(child, defs, weak, uses)


def def_use_for_node(ts: TSNode, kind: str) -> DefUse:
    """The def/use of one JS/TS CFG statement node (mirrors ``def_use.def_use_for_node``)."""
    defs: set[str] = set()
    weak: set[str] = set()
    uses: set[str] = set()

    if kind == "condition":  # an if/while predicate expression (header — body is separate)
        _reads(ts, defs, weak, uses)
    elif ts.type == "for_statement":  # header only: init + condition + increment
        for field in ("initializer", "condition", "increment"):
            part = ts.child_by_field_name(field)
            if part is None:
                continue
            if field == "condition":
                _reads(part, defs, weak, uses)
            elif part.type in _DECLS:
                _declaration(part, defs, weak, uses)
            else:
                _expr(part, defs, weak, uses)
    elif ts.type == "for_in_statement":  # header only: target + iterable
        left = ts.child_by_field_name("left")
        right = ts.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif ts.type in ("while_statement", "do_statement"):  # header only: condition
        cond = ts.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
    else:  # every other statement — aggregate the whole subtree (folds opaque try/switch)
        _collect_all(ts, defs, weak, uses)

    return DefUse(frozenset(uses), frozenset(defs), frozenset(weak))


def _pattern_names(pattern: TSNode) -> set[str]:
    """The binding name(s) of a (possibly destructuring) parameter/pattern, excluding defaults."""
    out: set[str] = set()
    stack = [pattern]
    while stack:
        cur = stack.pop()
        kind = cur.type
        if kind in ("identifier", "shorthand_property_identifier_pattern"):
            out.add(_name(cur))
        elif kind == "pair_pattern":
            value = cur.child_by_field_name("value")
            if value is not None:
                stack.append(value)
        elif kind in ("assignment_pattern", "object_assignment_pattern"):
            left = cur.child_by_field_name("left")  # the default value is not a binding
            if left is not None:
                stack.append(left)
        elif kind == "property_identifier":
            continue
        else:
            stack.extend(cur.named_children)
    return out


def parameter_defs(func: TSNode) -> frozenset[str]:
    """The parameter names a JS/TS function binds (strong defs at the CFG ENTRY)."""
    params = func.child_by_field_name("parameters")
    if params is None:  # arrow with a single bare param: ``x => …``
        single = func.child_by_field_name("parameter")
        return frozenset(_pattern_names(single)) if single is not None else frozenset()
    out: set[str] = set()
    for param in params.named_children:
        if param.type in ("required_parameter", "optional_parameter"):
            pat = param.child_by_field_name("pattern")
            if pat is not None:
                out |= _pattern_names(pat)
        else:
            out |= _pattern_names(param)
    return frozenset(out)
