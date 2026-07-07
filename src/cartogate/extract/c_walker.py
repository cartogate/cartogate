"""Tree-sitter AST walk for C → raw structural facts (F-08).

Deterministic and model-free, mirroring the other walkers. C is procedural — no classes, no
namespaces, no overloading — so the model is flat:

- **functions** are top-level symbols (only *definitions*, which carry a body; bare prototypes in
  headers are declarations, not symbols — a call resolves to the definition);
- **types** are top-level symbols: ``struct``/``union``/``enum`` *with a body*, and ``typedef``;
- name occurrences a resolver binds: ``#include "x.h"`` (import), direct ``f(...)`` calls, and
  ``struct T`` / typedef-name uses in a type position (references).

A ``.c`` or ``.h`` file is its own module (``file_is_namespace=True``): a function's qname is
``<file-module>.func``. C linkage is global, so the resolver indexes external functions by name
(statics stay file-local) — see ``resolver_c``. Function-pointer calls (``obj->fn()``) and
macro-hidden calls stay unresolved (sound ceiling — never a wrong edge).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_c as tsc
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_REFERENCE,
    FileFacts,
    RawName,
    RawSymbol,
)
from cartogate.extract.treesitter_util import body_hash
from cartogate.extract.treesitter_util import point as _point
from cartogate.extract.treesitter_util import text as _text
from cartogate.schema.enums import NodeKind, Visibility

#: The compiled C grammar, constructed once and shared by all walkers.
_C_LANGUAGE = Language(tsc.language())

#: ``struct``/``union``/``enum`` specifiers — a *definition* when they carry a body.
_TYPE_SPECIFIERS = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "function"


class CWalker:
    """Walks C source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_C_LANGUAGE)

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
        if node_type == "function_definition":
            self._function(node, stack, facts, excluded)
            return
        if node_type == "type_definition":
            self._typedef(node, stack, facts, excluded)
            return
        if node_type in _TYPE_SPECIFIERS and node.child_by_field_name("body") is not None:
            self._type_specifier(node, stack, facts, excluded)
            return
        if node_type == "call_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "type_identifier":
            self._type_reference(node, stack, facts, excluded)
            return
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _include(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        path = node.child_by_field_name("path")
        if path is None:
            return
        # ``"user.h"`` -> a repo-relative header (string_literal); ``<stdio.h>`` -> a system header
        # (system_lib_string), which the resolver leaves external. Carry the raw quoted/angled path.
        if path.type == "string_literal":
            inner = next((c for c in path.children if c.type == "string_content"), None)
            rel = _text(inner) if inner is not None else _text(path).strip('"')
            facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(path), rel, rel))
        else:  # system_lib_string -> external; name it by the header (``stdio.h`` -> ``stdio``)
            header = _text(path).strip("<>")
            facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(path), header, header))

    def _function(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node, params = _declarator_name_and_params(node.child_by_field_name("declarator"))
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name
        signature = f"{name}{_text(params) if params is not None else '()'}"

        facts.symbols.append(
            RawSymbol(
                kind=NodeKind.SYMBOL,
                name=name,
                qualified_name=qname,
                container_qname=container,
                signature=signature,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                visibility=_function_visibility(node),
                body_hash=body_hash(node.child_by_field_name("body")),
            )
        )
        stack.append(_Frame(qname, "function"))
        # Visit the parameter list (type references) and the body (calls / nested types).
        if params is not None:
            self._visit(params, stack, facts, excluded)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _typedef(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        # ``typedef struct {...} Point;`` — the new type name is the declarator (a type_identifier).
        name_node = node.child_by_field_name("declarator")
        while name_node is not None and name_node.type != "type_identifier":
            name_node = name_node.child_by_field_name("declarator") or _first_named(name_node)
        if name_node is not None and name_node.type == "type_identifier":
            self._emit_type(name_node, stack, facts, excluded)
        # Exclude an inner tag name (``typedef struct Foo {...} Foo;``) and recurse for nested refs.
        type_node = node.child_by_field_name("type")
        if type_node is not None and type_node.type in _TYPE_SPECIFIERS:
            inner = type_node.child_by_field_name("name")
            if inner is not None:
                excluded.add(_point(inner))
            body = type_node.child_by_field_name("body")
            if body is not None:
                self._visit(body, stack, facts, excluded)

    def _type_specifier(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        # A named ``struct/union/enum X {...}`` definition (with body).
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            self._emit_type(name_node, stack, facts, excluded)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)  # field types are references

    def _emit_type(
        self, name_node: TSNode, stack: list[_Frame], facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[0].qname  # types are module-level in C
        qname = f"{container}.{name}" if container else name
        facts.symbols.append(
            RawSymbol(
                kind=NodeKind.SYMBOL,
                name=name,
                qualified_name=qname,
                container_qname=container,
                signature=name,  # a type's gate signature is its bare name
                start_line=name_node.start_point[0] + 1,
                end_line=name_node.end_point[0] + 1,
                visibility=Visibility.EXPORTED,  # C types have no access control; treat as exported
            )
        )

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        func = node.child_by_field_name("function")
        if func is not None and func.type == "identifier":  # a direct ``f(...)`` call
            excluded.add(_point(func))
            facts.names.append(RawName(NAME_CALL, _enclosing(stack), *_point(func), _text(func)))
        # A ``obj->fn()`` / ``(*fp)()`` call has a non-identifier function — unresolved (ceiling).
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


def _declarator_name_and_params(decl: TSNode | None) -> tuple[TSNode | None, TSNode | None]:
    """Dig through pointer/parenthesized declarators to the ``function_declarator``; return its
    name identifier and parameter list. ``char *foo(int)`` nests the function under a pointer."""
    node = decl
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    if node is None:
        return None, None
    name = node.child_by_field_name("declarator")
    while name is not None and name.type not in ("identifier", "field_identifier"):
        name = name.child_by_field_name("declarator") or _first_named(name)
    return name, node.child_by_field_name("parameters")


def _function_visibility(node: TSNode) -> Visibility:
    """A ``static`` function is file-local (INTERNAL); else it has external linkage (EXPORTED)."""
    for child in node.children:
        if child.type == "storage_class_specifier" and _text(child) == "static":
            return Visibility.INTERNAL
    return Visibility.EXPORTED


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind == "function":
            return frame.qname
    return stack[0].qname


def _first_named(node: TSNode) -> TSNode | None:
    return next((c for c in node.children if c.is_named), None)
