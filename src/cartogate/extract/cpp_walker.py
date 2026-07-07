"""Tree-sitter AST walk for C++ → raw structural facts (F-08).

C++ combines C's procedural core with namespaces, classes, and out-of-line method definitions, so
this walker borrows from both the C and C# walkers:

- **classes/structs** (with a body) are top-level type symbols; their **methods** are nested
  symbols. Only method *definitions* become symbols — an in-class inline definition, or an
  out-of-line ``Ret Class::method(...) { ... }`` (a bare declaration is skipped, like a C
  prototype). With the C module model (a ``.hpp``/``.cpp`` pair of the same stem collapses to one
  module), a class declared in the header and its methods defined out-of-line in the source land
  under the same module.
- **free functions** are top-level symbols (``static`` → internal linkage).
- a ``namespace X {}`` block is **transparent** to qnames here (qnames stay file-module-based); the
  *resolver* reads the declared namespace + ``using`` to bind ``X::y`` references.
- name occurrences a resolver binds: ``#include``, base classes (inherits), ``new T``, and calls —
  unqualified ``f()``, ``obj.m()``/``obj->m()`` (declared-receiver), and ``Scope::name()``.

Templates, overloaded-call disambiguation, ADL, and function-pointer calls are ceilings — left
unresolved (sound, never a wrong edge).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
    FileFacts,
    RawName,
    RawSymbol,
)
from cartogate.extract.treesitter_util import body_hash
from cartogate.extract.treesitter_util import point as _point
from cartogate.extract.treesitter_util import text as _text
from cartogate.schema.enums import NodeKind, Visibility

#: The compiled C++ grammar, constructed once and shared by all walkers.
_CPP_LANGUAGE = Language(tscpp.language())

_CLASS_SPECIFIERS = frozenset({"class_specifier", "struct_specifier"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "class" | "function"


class CppWalker:
    """Walks C++ source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_CPP_LANGUAGE)

    def walk(self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str) -> FileFacts:
        facts = FileFacts(module_qname=module_qname, rel_path=rel_path, abs_path=abs_path)
        excluded: set[tuple[int, int]] = set()
        self._visit(self._parser.parse(source).root_node, [_Frame(module_qname, "module")],
                    facts, excluded)
        return facts

    # --- traversal ---------------------------------------------------------- #

    def _visit(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        node_type = node.type
        if node_type == "preproc_include":
            self._include(node, stack[0].qname, facts, excluded)
            return
        if node_type in _CLASS_SPECIFIERS and node.child_by_field_name("body") is not None:
            self._class(node, stack, facts, excluded)
            return
        if node_type == "function_definition":
            self._function(node, stack, facts, excluded)
            return
        if node_type == "call_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "new_expression":
            self._new(node, stack, facts, excluded)
            return
        if node_type == "type_identifier":
            self._type_reference(node, stack, facts, excluded)
            return
        # namespace_definition / declaration_list / anything else: descend (namespace is invisible).
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _include(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        path = node.child_by_field_name("path")
        if path is None:
            return
        if path.type == "string_literal":
            inner = next((c for c in path.children if c.type == "string_content"), None)
            rel = _text(inner) if inner is not None else _text(path).strip('"')
            facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(path), rel, rel))
        else:  # <system> header -> external
            header = _text(path).strip("<>")
            facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(path), header, header))

    def _class(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # an anonymous struct/class — skip (no gate-able symbol)
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[0].qname  # types are module-level (namespace transparent)
        qname = f"{container}.{name}" if container else name
        self._record_bases(node, qname, facts, excluded)
        facts.symbols.append(_symbol(name, qname, container, name, node, Visibility.EXPORTED))
        stack.append(_Frame(qname, "class"))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                self._visit(child, stack, facts, excluded)  # inline method defs + member types
        stack.pop()

    def _record_bases(
        self, node: TSNode, type_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        clause = next((c for c in node.children if c.type == "base_class_clause"), None)
        if clause is None:
            return
        for child in clause.children:
            if child.type == "type_identifier":
                excluded.add(_point(child))
                facts.names.append(RawName(NAME_INHERIT, type_qname, *_point(child), _text(child)))

    def _function(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node, params, scope = _function_name(node.child_by_field_name("declarator"))
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        if scope is not None:  # out-of-line ``Ret Class::method(...)`` — class is the scope's tail
            class_name = scope.split("::")[-1]
            container = f"{stack[0].qname}.{class_name}" if stack[0].qname else class_name
            visibility = Visibility.INTERNAL
        elif stack[-1].kind == "class":  # an inline method definition
            container = stack[-1].qname
            visibility = Visibility.INTERNAL
        else:  # a free function
            container = stack[0].qname
            visibility = Visibility.INTERNAL if _is_static(node) else Visibility.EXPORTED
        qname = f"{container}.{name}" if container else name
        signature = f"{name}{_text(params) if params is not None else '()'}"
        facts.symbols.append(
            _symbol(name, qname, container, signature, node, visibility,
                    body_hash(node.child_by_field_name("body")))
        )
        stack.append(_Frame(qname, "function"))
        if params is not None:
            self._visit(params, stack, facts, excluded)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        func = node.child_by_field_name("function")
        name_node: TSNode | None = None
        recurse_receiver: TSNode | None = None
        if func is not None:
            if func.type == "identifier":
                name_node = func  # unqualified ``f(...)``
            elif func.type == "field_expression":  # ``obj.m()`` / ``obj->m()``
                name_node = func.child_by_field_name("field")
                recurse_receiver = func.child_by_field_name("argument")
            elif func.type == "qualified_identifier":  # ``Scope::name(...)``
                name_node = _qualified_tail(func)
        if name_node is not None:
            excluded.add(_point(name_node))
            facts.names.append(RawName(NAME_CALL, _enclosing(stack), *_point(name_node),
                                       _text(name_node)))
        if recurse_receiver is not None:
            self._visit(recurse_receiver, stack, facts, excluded)
        args = node.child_by_field_name("arguments")
        if args is not None:
            self._visit(args, stack, facts, excluded)

    def _new(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            type_id = type_node if type_node.type == "type_identifier" else _last_type_id(type_node)
            if type_id is not None:
                excluded.add(_point(type_id))
                facts.names.append(RawName(NAME_CALL, _enclosing(stack), *_point(type_id),
                                           _text(type_id)))
        args = node.child_by_field_name("arguments")
        if args is not None:
            self._visit(args, stack, facts, excluded)

    def _type_reference(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        point = _point(node)
        if point in excluded:
            return
        excluded.add(point)
        facts.names.append(RawName(NAME_REFERENCE, _enclosing(stack), *point, _text(node)))


# --- helpers ---------------------------------------------------------------- #


def _symbol(
    name: str, qname: str, container: str, signature: str, node: TSNode,
    visibility: Visibility, bhash: str | None = None,
) -> RawSymbol:
    return RawSymbol(
        kind=NodeKind.SYMBOL,
        name=name,
        qualified_name=qname,
        container_qname=container,
        signature=signature,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        visibility=visibility,
        body_hash=bhash,
    )


def _function_name(decl: TSNode | None) -> tuple[TSNode | None, TSNode | None, str | None]:
    """Return ``(name_node, parameters, scope)`` for a function definition's declarator.

    ``scope`` is the ``Class`` (or ``Ns::Class``) of an out-of-line ``Class::method`` definition,
    else ``None``. Digs through pointer/reference declarators to the ``function_declarator``.
    """
    node = decl
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    if node is None:
        return None, None, None
    params = node.child_by_field_name("parameters")
    name = node.child_by_field_name("declarator")
    if name is not None and name.type == "qualified_identifier":
        tail = _qualified_tail(name)
        scope = _text(name).rsplit("::", 1)[0]
        return tail, params, scope
    while name is not None and name.type not in ("identifier", "field_identifier",
                                                 "destructor_name", "operator_name"):
        name = name.child_by_field_name("declarator") or _first_named(name)
    return name, params, None


def _qualified_tail(node: TSNode) -> TSNode | None:
    """The trailing name of a ``qualified_identifier`` (``a::b::c`` -> the ``c`` identifier)."""
    name = node.child_by_field_name("name")
    while name is not None and name.type == "qualified_identifier":
        name = name.child_by_field_name("name")
    return name


def _last_type_id(node: TSNode) -> TSNode | None:
    found: TSNode | None = None
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "type_identifier" and (found is None or cur.start_point > found.start_point):
            found = cur
        stack.extend(cur.children)
    return found


def _is_static(node: TSNode) -> bool:
    return any(c.type == "storage_class_specifier" and _text(c) == "static" for c in node.children)


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind in ("class", "function"):
            return frame.qname
    return stack[0].qname


def _first_named(node: TSNode) -> TSNode | None:
    return next((c for c in node.children if c.is_named), None)
