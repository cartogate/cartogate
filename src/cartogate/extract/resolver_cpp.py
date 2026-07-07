"""Pure-Python C++ name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for C++. It parses each
file once and builds three name indexes over the repo's *definitions* (bodies):

- **types** — classes/structs by (bare) name → definition; a base class / ``new T`` / type use binds
  to the unique in-repo class of that name (ambiguous duplicates → unresolved).
- **methods** — ``(class, method)`` → definition, gathered from in-class inline definitions and
  out-of-line ``Ret Class::method(...) {}`` definitions.
- **free functions** — by name → definition (unique; an overloaded name is ambiguous → unresolved).

Resolution binds: ``#include "x.h"`` → the in-repo header module; a base/`new`/type use → its class;
an unqualified ``f()`` → the enclosing class's method (inside a method) else a unique free function;
``obj.m()``/``obj->m()`` → the method of ``obj``'s *declared* class type (read, not inferred); and
``Scope::name()`` → ``Scope``'s method if ``Scope`` is a class, else a free function.

Ceilings (return ``None`` → no edge): overloaded targets, templates / dependent types, ADL, a
receiver of inferred/`auto` type, and function-pointer calls. Sound — never a wrong edge.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_CPP_LANGUAGE = Language(tscpp.language())
_CLASS_SPECIFIERS = frozenset({"class_specifier", "struct_specifier"})
_AMBIGUOUS = ("", -1)


class CppResolver:
    """Resolves C++ name occurrences against in-repo type/method/free-function indexes."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_CPP_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._types: dict[str, tuple[str, int]] = {}  # name -> (abs, line)
        self._methods: dict[tuple[str, str], tuple[str, int]] = {}  # (class, method) -> (abs, line)
        self._free: dict[str, tuple[str, int]] = {}  # free-function name -> (abs, line)
        self._abspaths = {str(Path(p).resolve()) for p in sources}
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        self._walk_defs(root, abs_path)

    def _walk_defs(self, node: TSNode, abs_path: str) -> None:
        for child in node.children:
            if child.type in _CLASS_SPECIFIERS and child.child_by_field_name("body") is not None:
                self._index_class(child, abs_path)
            elif child.type == "function_definition":
                self._index_function(child, abs_path)
            else:
                self._walk_defs(child, abs_path)  # descend into namespaces / declaration lists

    def _index_class(self, node: TSNode, abs_path: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(name_node)
        self._types[name] = _AMBIGUOUS if name in self._types else (
            abs_path, name_node.start_point[0] + 1
        )
        body = node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type == "function_definition":  # an inline method definition
                m_name, _, _ = _function_name(member.child_by_field_name("declarator"))
                if m_name is not None:
                    self._add_method(name, _text(m_name), abs_path, member.start_point[0] + 1)

    def _index_function(self, node: TSNode, abs_path: str) -> None:
        name_node, _, scope = _function_name(node.child_by_field_name("declarator"))
        if name_node is None:
            return
        line = node.start_point[0] + 1
        if scope is not None:  # out-of-line ``Class::method``
            self._add_method(scope.split("::")[-1], _text(name_node), abs_path, line)
        else:  # a free function (overloads -> ambiguous)
            name = _text(name_node)
            self._free[name] = _AMBIGUOUS if name in self._free else (abs_path, line)

    def _add_method(self, cls: str, method: str, abs_path: str, line: int) -> None:
        key = (cls, method)
        self._methods[key] = _AMBIGUOUS if key in self._methods else (abs_path, line)

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        include = _ancestor(node, {"preproc_include"})
        if include is not None:
            return self._resolve_include(include, abs_path)

        if _ancestor(node, {"new_expression"}) is not None:
            return self._resolve_type(_text(node))

        call = _ancestor(node, {"call_expression"})
        if call is not None and _is_call_name(call, node):
            return self._resolve_call(call, node, abs_path)

        if node.type == "type_identifier":
            return self._resolve_type(_text(node))
        return None

    def _resolve_include(self, include: TSNode, abs_path: str) -> Resolved | None:
        path = include.child_by_field_name("path")
        if path is None or path.type != "string_literal":
            return None
        inner = next((c for c in path.children if c.type == "string_content"), None)
        rel = _text(inner) if inner is not None else _text(path).strip('"')
        target = (Path(abs_path).parent / rel).resolve()
        if str(target) in self._abspaths:
            return Resolved(target.name, None, target, 1, "module")
        return None

    def _resolve_type(self, name: str) -> Resolved | None:
        hit = self._types.get(name.split("::")[-1])
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(name, name, Path(hit[0]), hit[1], "class")

    def _resolve_call(self, call: TSNode, node: TSNode, abs_path: str) -> Resolved | None:
        func = call.child_by_field_name("function")
        if func is None:
            return None
        method = _text(node)

        if func.type == "identifier":  # unqualified ``f()``
            cls = _enclosing_class(call, abs_path, self._trees.get(abs_path))
            if cls is not None and (cls, method) in self._methods:
                return self._method(cls, method)
            return self._free_fn(method)

        if func.type == "qualified_identifier":  # ``Scope::name()``
            scope = _text(func).rsplit("::", 1)[0].split("::")[-1]
            if (scope, method) in self._methods:  # Scope is a class -> a (static) method
                return self._method(scope, method)
            return self._free_fn(method)  # Scope is a namespace -> a free function

        if func.type == "field_expression":  # ``obj.m()`` / ``obj->m()``
            receiver = func.child_by_field_name("argument")
            if receiver is not None and receiver.type == "identifier":
                cls = _declared_type(receiver, _text(receiver))
                if cls is not None and (cls, method) in self._methods:
                    return self._method(cls, method)
        return None

    def _method(self, cls: str, method: str) -> Resolved | None:
        hit = self._methods[(cls, method)]
        if hit == _AMBIGUOUS:  # an overloaded method — can't pick one without arg types
            return None
        return Resolved(method, f"{cls}::{method}", Path(hit[0]), hit[1], "function")

    def _free_fn(self, name: str) -> Resolved | None:
        hit = self._free.get(name)
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(name, name, Path(hit[0]), hit[1], "function")


# --- helpers ---------------------------------------------------------------- #


def _enclosing_class(node: TSNode, abs_path: str, root: TSNode | None) -> str | None:
    """The class enclosing an unqualified call: an out-of-line method's ``Class::`` qualifier, else
    the nearest ``class``/``struct`` definition's name."""
    cur: TSNode | None = node
    while cur is not None:
        if cur.type == "function_definition":
            _, _, scope = _function_name(cur.child_by_field_name("declarator"))
            if scope is not None:
                return scope.split("::")[-1]
        if cur.type in _CLASS_SPECIFIERS:
            name = cur.child_by_field_name("name")
            return _text(name) if name is not None else None
        cur = cur.parent
    return None


def _declared_type(obj: TSNode, name: str) -> str | None:
    """The declared class type of variable ``name`` in the nearest enclosing scope, or ``None``.

    Reads an explicit ``Type name`` parameter, field, or local (``Type v;`` / ``Type *v = ...`` /
    ``Type v(args)``) — never inferred (``auto``)."""
    before = obj.start_point
    scope: TSNode | None = obj.parent
    while scope is not None:
        found = _scope_declares(scope, name, before)
        if found is not None:
            return found
        scope = scope.parent
    return None


def _scope_declares(scope: TSNode, name: str, before: tuple[int, int]) -> str | None:
    params = scope.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type == "parameter_declaration" and _declarator_name_text(param) == name:
                return _type_name(param.child_by_field_name("type"))
    for child in scope.children:
        is_field = child.type == "field_declaration"
        is_local = child.type == "declaration" and child.start_point < before
        if (is_field or is_local) and _declarator_name_text(child) == name:
            return _type_name(child.child_by_field_name("type"))
    return None


#: Declarator wrappers we dig through to the bound name (``*``/``&``/``[]``/init/``Type v(args)``).
_DECL_WRAPPERS = frozenset({
    "init_declarator", "pointer_declarator", "reference_declarator", "array_declarator",
    "function_declarator",
})


def _declarator_name_text(node: TSNode) -> str | None:
    """The variable name of a parameter/field/local declaration (digs through ``*``/``&``/init)."""
    decl = node.child_by_field_name("declarator")
    while decl is not None and decl.type not in ("identifier", "field_identifier"):
        if decl.type in _DECL_WRAPPERS:
            decl = decl.child_by_field_name("declarator")
        else:
            decl = _first_named(decl)
    return _text(decl) if decl is not None else None


def _type_name(type_node: TSNode | None) -> str | None:
    """The simple class name of a type node, or ``None`` for primitives/``auto``/unknowns."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        return _text(type_node)
    if type_node.type == "qualified_identifier":  # ``ns::User`` -> ``User``
        return _text(type_node).split("::")[-1]
    return None  # primitive_type / auto / template / placeholder -> not an in-repo class receiver


def _function_name(decl: TSNode | None) -> tuple[TSNode | None, TSNode | None, str | None]:
    node = decl
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    if node is None:
        return None, None, None
    params = node.child_by_field_name("parameters")
    name = node.child_by_field_name("declarator")
    if name is not None and name.type == "qualified_identifier":
        tail = _qualified_tail(name)
        return tail, params, _text(name).rsplit("::", 1)[0]
    while name is not None and name.type not in ("identifier", "field_identifier",
                                                 "destructor_name", "operator_name"):
        name = name.child_by_field_name("declarator") or _first_named(name)
    return name, params, None


def _qualified_tail(node: TSNode) -> TSNode | None:
    name = node.child_by_field_name("name")
    while name is not None and name.type == "qualified_identifier":
        name = name.child_by_field_name("name")
    return name


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_call_name(call: TSNode, node: TSNode) -> bool:
    func = call.child_by_field_name("function")
    if func is None:
        return False
    if func.type == "identifier":
        return func.start_point == node.start_point
    if func.type == "field_expression":
        field = func.child_by_field_name("field")
        return field is not None and field.start_point == node.start_point
    if func.type == "qualified_identifier":
        tail = _qualified_tail(func)
        return tail is not None and tail.start_point == node.start_point
    return False


def _first_named(node: TSNode) -> TSNode | None:
    return next((c for c in node.children if c.is_named), None)
