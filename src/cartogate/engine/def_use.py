"""Per-statement variable def/use for Python (F-03, PDG data-dependence input).

For one CFG statement node, which variables it **defines** vs **uses**. The output feeds reaching-
definitions; the cardinal property is that a real dependence is never missed, so this
**over-approximates** toward inclusion.

Two def flavours — the soundness linchpin:

- ``defs`` (**strong**): a clean rebinding of a plain name (``x = …``, a ``for`` target, a ``with …
  as x``, an ``except … as e``, an ``import``, a parameter, ``global``/``nonlocal``). A strong def
  both *generates* a definition and *kills* prior defs of that name.
- ``weak_defs`` (**weak**): a *possible* modification/binding that may not fully rebind, or that we
  can't cleanly attribute — a mutation through an attribute/subscript (``a.b = …``), a name passed
  to / the receiver of a call, or a ``match`` capture. A weak def *generates* but does **not** kill,
  so neither an earlier clean def nor the mutation is lost downstream.

``uses`` collects every name read anywhere in the statement (descending into comprehensions/lambdas
as an over-approximation). The three CFG-modelled headers (an ``if``/``elif`` condition, a ``for``
header, a ``while`` header) are read *header-only* — their bodies are separate CFG nodes. Every
other statement is aggregated over its **whole subtree** by ``_collect_all``, because the CFG leaves
``with``/``try``/``match`` opaque (one node spanning the body), so their internal defs/uses must be
folded in or they would be missed.

Residual under-approximation, out of scope for intraprocedural v1 (documented): aliasing mutation
(``b = a; b.x = 1``), ``exec``/``eval``/``globals()``, and ``import *`` (unknown bound names).
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node as TSNode


@dataclass(frozen=True, slots=True)
class DefUse:
    """The variables a statement reads (``uses``), strongly rebinds (``defs``, gen+kill), and may
    modify without a clean rebind (``weak_defs``, gen-only)."""

    uses: frozenset[str]
    defs: frozenset[str]
    weak_defs: frozenset[str]


def _name(node: TSNode) -> str:
    return (node.text or b"").decode("utf-8", "replace")


def _idents(node: TSNode) -> set[str]:
    """Root identifiers of an lvalue expression (``a`` of ``a.b``, ``a`` and ``i`` of ``a[i]``)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            out.add(_name(cur))
        elif cur.type == "attribute":
            obj = cur.child_by_field_name("object")
            if obj is not None:
                stack.append(obj)  # the property name is not a variable
        else:
            stack.extend(cur.named_children)
    return out


def _all_idents(node: TSNode) -> set[str]:
    """Every identifier in a subtree (for match patterns, where any name may be a capture)."""
    out: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            out.add(_name(cur))
        stack.extend(cur.named_children)
    return out


def _collect_reads(node: TSNode, uses: set[str], weak: set[str]) -> None:
    """Add every name *read* in ``node`` to ``uses``; record call-mutation weak defs (a call may
    mutate its receiver / a name passed as an argument)."""
    kind = node.type
    if kind == "identifier":
        uses.add(_name(node))
        return
    if kind == "attribute":  # a.b -> read a, not the property name
        obj = node.child_by_field_name("object")
        if obj is not None:
            _collect_reads(obj, uses, weak)
        return
    if kind == "keyword_argument":  # key=value -> read value, not the key
        value = node.child_by_field_name("value")
        if value is not None:
            _collect_reads(value, uses, weak)
        return
    if kind == "call":
        fn = node.child_by_field_name("function")
        if fn is not None:
            _collect_reads(fn, uses, weak)
            if fn.type == "attribute":  # obj.m(...) — the callee may mutate obj
                obj = fn.child_by_field_name("object")
                if obj is not None:
                    weak |= _idents(obj)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                _collect_reads(arg, uses, weak)
                # the callee may mutate a name passed in (plain, *splat, or **splat)
                if arg.type in ("identifier", "list_splat", "dictionary_splat"):
                    weak |= _idents(arg)
        return
    for child in node.named_children:
        _collect_reads(child, uses, weak)


def _target(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Classify an assignment target: a plain name is a strong def; an attribute/subscript is a weak
    def of its base (+ a read of the base/index); tuple/list/splat patterns recurse."""
    kind = node.type
    if kind == "identifier":
        defs.add(_name(node))
    elif kind in (
        "tuple_pattern", "list_pattern", "pattern_list", "tuple", "list",  # x, y = ...
        "list_splat_pattern", "list_splat",  # *rest = ...
        "as_pattern_target",  # with .. as (target) / except .. as (target)
    ):
        for child in node.named_children:
            _target(child, defs, weak, uses)
    elif kind == "attribute":  # a.b = ... -> weak-def + read a
        obj = node.child_by_field_name("object")
        if obj is not None:
            weak |= _idents(obj)
            _collect_reads(obj, uses, weak)
    elif kind == "subscript":  # a[i] = ... -> weak-def a; read a and i
        value = node.child_by_field_name("value")
        if value is not None:
            weak |= _idents(value)
        for child in node.named_children:
            _collect_reads(child, uses, weak)
    else:
        _collect_reads(node, uses, weak)


def _walrus_defs(node: TSNode, defs: set[str]) -> None:
    """Strong defs from any ``(name := expr)`` anywhere in an expression."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "named_expression":
            name = cur.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                defs.add(_name(name))
        stack.extend(cur.named_children)


def _reads(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Read an arbitrary expression: uses + call-mutation weak defs + any walrus strong def."""
    _collect_reads(node, uses, weak)
    _walrus_defs(node, defs)


def _import_defs(node: TSNode) -> set[str]:
    """Names an ``import`` binds (the alias if present, else the top module / imported name;
    ``import *`` is out of scope — unknown names)."""
    out: set[str] = set()
    module = node.child_by_field_name("module_name")  # the `from <module>` part (None for `import`)
    module_start = module.start_byte if module is not None else -1
    for child in node.named_children:
        if child.start_byte == module_start:
            continue  # the `from <module>` part binds nothing
        if child.type == "aliased_import":
            alias = child.child_by_field_name("alias")
            if alias is not None:
                out.add(_name(alias))
        elif child.type == "dotted_name" and child.named_children:
            out.add(_name(child.named_children[0]))  # import os.path->os; from m import bar->bar
    return out


def _expression(inner: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    if inner.type == "assignment":
        left = inner.child_by_field_name("left")
        annotation = inner.child_by_field_name("type")
        right = inner.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if annotation is not None:
            _reads(annotation, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif inner.type == "augmented_assignment":  # x += e -> x is def AND use; a.b += e -> weak + use
        left = inner.child_by_field_name("left")
        right = inner.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
            _collect_reads(left, uses, weak)
        if right is not None:
            _reads(right, defs, weak, uses)
    else:  # a bare expression / call statement
        _reads(inner, defs, weak, uses)


def _with_clause(clause: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    for item in clause.named_children:  # with_item nodes
        inner = item.named_children[0] if item.named_children else None
        if inner is None:
            continue
        if inner.type == "as_pattern":
            value = inner.named_children[0] if inner.named_children else None
            alias = inner.child_by_field_name("alias")
            if value is not None:
                _reads(value, defs, weak, uses)
            if alias is not None:
                _target(alias, defs, weak, uses)
        else:
            _reads(inner, defs, weak, uses)


def _collect_all(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Aggregate defs+uses over a whole statement subtree. Used for every statement except the three
    CFG-modelled headers; folds in the bodies of ``with``/``try``/``match`` (opaque in the CFG)."""
    kind = node.type
    if kind == "block":
        for child in node.named_children:
            if child.type != "comment":
                _collect_all(child, defs, weak, uses)
    elif kind == "expression_statement":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _expression(inner, defs, weak, uses)
    elif kind in ("assignment", "augmented_assignment"):
        _expression(node, defs, weak, uses)
    elif kind in ("import_statement", "import_from_statement"):
        defs |= _import_defs(node)
    elif kind in ("global_statement", "nonlocal_statement"):
        for child in node.named_children:
            if child.type == "identifier":
                ident = _name(child)
                defs.add(ident)
                uses.add(ident)
    elif kind in ("function_definition", "class_definition"):
        name = node.child_by_field_name("name")
        if name is not None:
            defs.add(_name(name))
        captured: set[str] = set()
        _collect_reads(node, captured, weak)  # opaque capture: every inner read is a use
        if name is not None:
            captured.discard(_name(name))
        uses |= captured
    elif kind == "decorated_definition":
        for child in node.named_children:
            _collect_all(child, defs, weak, uses)  # decorators (reads) + the inner def
    elif kind == "for_statement":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind in ("while_statement", "if_statement", "elif_clause"):
        cond = node.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "with_statement":
        for child in node.children:
            if child.type == "with_clause":
                _with_clause(child, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)
    elif kind == "except_clause":
        for child in node.named_children:
            if child.type == "as_pattern":  # except E as e
                value = child.named_children[0] if child.named_children else None
                alias = child.child_by_field_name("alias")
                if value is not None:
                    _reads(value, defs, weak, uses)
                if alias is not None:
                    _target(alias, defs, weak, uses)
            elif child.type == "block":
                _collect_all(child, defs, weak, uses)
            else:
                _reads(child, defs, weak, uses)  # the bare exception type
    elif kind == "case_clause":
        for child in node.named_children:
            if child.type == "case_pattern":  # captures are weak (no kill) — pattern names may bind
                idents = _all_idents(child)
                weak |= idents
                uses |= idents
            elif child.type == "block":
                _collect_all(child, defs, weak, uses)
            else:
                _reads(child, defs, weak, uses)  # a guard `if ...`
    else:  # try/match subject, return/raise/assert/del/pass: reads + recurse nested bodies
        _reads(node, defs, weak, uses)
        _recurse_blocks(node, defs, weak, uses)


def _recurse_blocks(node: TSNode, defs: set[str], weak: set[str], uses: set[str]) -> None:
    """Recurse into the nested suites/clauses of a compound statement (its body lives in this CFG
    node when the construct is CFG-opaque), so their defs/uses are folded in."""
    for child in node.named_children:
        if child.type == "block" or child.type.endswith("_clause"):
            _collect_all(child, defs, weak, uses)


def def_use_for_node(ts: TSNode, kind: str) -> DefUse:
    """The def/use of one CFG statement node (``ts`` its tree-sitter node; ``kind`` its kind)."""
    defs: set[str] = set()
    weak: set[str] = set()
    uses: set[str] = set()

    if kind == "condition":  # an if/elif predicate expression (header — body is separate)
        _reads(ts, defs, weak, uses)
    elif ts.type == "for_statement":  # header only: target + iterable (body is separate CFG nodes)
        left = ts.child_by_field_name("left")
        right = ts.child_by_field_name("right")
        if left is not None:
            _target(left, defs, weak, uses)
        if right is not None:
            _reads(right, defs, weak, uses)
    elif ts.type == "while_statement":  # header only: condition
        cond = ts.child_by_field_name("condition")
        if cond is not None:
            _reads(cond, defs, weak, uses)
    else:  # every other statement — aggregate the whole subtree (folds opaque with/try/match)
        _collect_all(ts, defs, weak, uses)

    return DefUse(frozenset(uses), frozenset(defs), frozenset(weak))


def parameter_defs(func: TSNode) -> frozenset[str]:
    """The parameter names a ``function_definition`` binds (strong defs live at the CFG ENTRY)."""
    params = func.child_by_field_name("parameters")
    if params is None:
        return frozenset()
    out: set[str] = set()
    for param in params.named_children:
        out |= _param_name(param)
    return frozenset(out)


def _param_name(param: TSNode) -> set[str]:
    """The name(s) a single parameter binds, excluding its type annotation and default value."""
    if param.type == "identifier":
        return {_name(param)}
    if param.type in ("list_splat_pattern", "dictionary_splat_pattern"):
        return _idents(param)
    # typed / default / typed-default param: the binding is the first identifier / splat child;
    # the `type` and `value` fields (which follow) must NOT be counted as defs.
    for child in param.children:
        if child.type == "identifier":
            return {_name(child)}
        if child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            return _idents(child)
    return set()
