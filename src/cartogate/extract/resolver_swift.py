"""Pure-Python Swift name resolver (F-08) — in-process, deterministic, air-gapped.

Swift has a flat module namespace (types/functions are visible repo-wide without imports), so —
like the C resolver — resolution is a global name index rather than a scope walk:

- **types** by name → definition (ambiguous duplicates → unresolved). Binds supertypes, type uses,
  and **initializer calls** ``User(...)`` (Swift has no ``new``).
- **methods** ``(type, method)`` → definition, gathered from class/struct/enum/protocol bodies and
  ``extension Type {}`` blocks.
- **top-level functions** by name → definition (overloads → unresolved).

Calls bind: an unqualified ``f()`` → a same-type method, else a unique top-level function (or an
initializer if ``f`` is a type); ``self.m()`` → the enclosing type's method; ``obj.m()`` → the
method of ``obj``'s *declared* type (a ``x: T`` parameter/property — read, not inferred);
``Type.m()`` → that type's method.

Ceilings (``None`` → no edge): inferred receivers (``let x = make()``), protocol-witness dispatch,
overloaded targets, ``import``ed (out-of-module) symbols, and anything outside the repo.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_swift as tsswift
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_SWIFT_LANGUAGE = Language(tsswift.language())
_AMBIGUOUS = ("", -1)


class SwiftResolver:
    """Resolves Swift name occurrences against in-repo type/method/function indexes."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_SWIFT_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._types: dict[str, tuple[str, int]] = {}  # name -> (abs, line)
        self._methods: dict[tuple[str, str], tuple[str, int]] = {}  # (type, method) -> (abs, line)
        self._functions: dict[str, tuple[str, int]] = {}  # name -> (abs, line); top-level
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        self._walk_defs(root, abs_path, None)

    def _walk_defs(self, node: TSNode, abs_path: str, enclosing_type: str | None) -> None:
        for child in node.children:
            if child.type in ("class_declaration", "protocol_declaration"):
                self._index_type(child, abs_path)
            elif child.type == "function_declaration" and enclosing_type is None:
                name = next((c for c in child.children if c.type == "simple_identifier"), None)
                if name is not None:
                    key = _text(name)
                    line = child.start_point[0] + 1
                    self._functions[key] = (
                        _AMBIGUOUS if key in self._functions else (abs_path, line)
                    )
            else:
                self._walk_defs(child, abs_path, enclosing_type)

    def _index_type(self, node: TSNode, abs_path: str) -> None:
        is_extension = any(not c.is_named and _text(c) == "extension" for c in node.children)
        name_node = next((c for c in node.children if c.type == "type_identifier"), None)
        if is_extension or name_node is None:
            type_name = _extension_target(node)
            if type_name is None:
                return
        else:
            type_name = _text(name_node)
            line = name_node.start_point[0] + 1
            self._types[type_name] = _AMBIGUOUS if type_name in self._types else (abs_path, line)
        body = next((c for c in node.children if c.type in ("class_body", "protocol_body",
                                                            "enum_class_body")), None)
        if body is None:
            return
        for member in body.children:
            if member.type == "function_declaration":
                m_name = next((c for c in member.children if c.type == "simple_identifier"), None)
                if m_name is not None:
                    self._add_method(type_name, _text(m_name), abs_path, member.start_point[0] + 1)
            elif member.type in ("class_declaration", "protocol_declaration"):
                self._index_type(member, abs_path)  # nested type

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

        if _ancestor(node, {"import_declaration"}) is not None:
            return None  # Swift imports are other modules — external, never in-repo
        call = _ancestor(node, {"call_expression"})
        if call is not None and _is_call_name(call, node):
            return self._resolve_call(node, abs_path)
        if node.type == "type_identifier":
            return self._resolve_type(_text(node))
        return None

    def _resolve_call(self, node: TSNode, abs_path: str) -> Resolved | None:
        name = _text(node)
        parent = node.parent
        if parent is not None and parent.type == "navigation_suffix":
            return self._resolve_member_call(parent.parent, node, name, abs_path)
        # Unqualified ``name(...)`` — an initializer, a same-type method, or a top-level function.
        if name in self._types and self._types[name] != _AMBIGUOUS:
            hit = self._types[name]
            return Resolved(name, name, Path(hit[0]), hit[1], "class")  # initializer call
        enclosing = _enclosing_type(node)
        if enclosing is not None and (enclosing, name) in self._methods:
            return self._method(enclosing, name)
        return self._free_function(name)

    def _resolve_member_call(
        self, nav: TSNode | None, node: TSNode, method: str, abs_path: str
    ) -> Resolved | None:
        if nav is None:
            return None
        receiver = next(
            (c for c in nav.children if c.is_named and c.type != "navigation_suffix"), None
        )
        if receiver is None:
            return None
        if receiver.type == "self_expression":
            enclosing = _enclosing_type(node)
            if enclosing is not None and (enclosing, method) in self._methods:
                return self._method(enclosing, method)
            return None
        if receiver.type == "simple_identifier":
            cls = _text(receiver)  # ``Type.m()`` — receiver is itself a type...
            if (cls, method) not in self._methods:  # ...else ``var.m()`` with a declared type
                declared = _declared_type(receiver, _text(receiver))
                cls = declared if declared is not None else cls
            if (cls, method) in self._methods:
                return self._method(cls, method)
        return None

    def _resolve_type(self, name: str) -> Resolved | None:
        simple = name.rsplit(".", 1)[-1]
        hit = self._types.get(simple)
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(simple, simple, Path(hit[0]), hit[1], "class")

    def _method(self, cls: str, method: str) -> Resolved | None:
        hit = self._methods[(cls, method)]
        if hit == _AMBIGUOUS:
            return None
        return Resolved(method, f"{cls}.{method}", Path(hit[0]), hit[1], "function")

    def _free_function(self, name: str) -> Resolved | None:
        hit = self._functions.get(name)
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(name, name, Path(hit[0]), hit[1], "function")


# --- helpers ---------------------------------------------------------------- #


def _extension_target(node: TSNode) -> str | None:
    user_type = next((c for c in node.children if c.type == "user_type"), None)
    if user_type is None:
        return None
    ids = [c for c in user_type.children if c.type == "type_identifier"]
    return _text(ids[-1]) if ids else None


def _enclosing_type(node: TSNode) -> str | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in ("class_declaration", "protocol_declaration"):
            is_extension = any(not c.is_named and _text(c) == "extension" for c in cur.children)
            if is_extension:
                return _extension_target(cur)
            name = next((c for c in cur.children if c.type == "type_identifier"), None)
            return _text(name) if name is not None else None
        cur = cur.parent
    return None


def _declared_type(obj: TSNode, name: str) -> str | None:
    """The declared type of variable ``name`` in the nearest enclosing scope, or ``None`` (a Swift
    ``label name: Type`` parameter or a ``let``/``var name: Type`` property — never inferred)."""
    before = obj.start_point
    scope: TSNode | None = obj.parent
    while scope is not None:
        found = _scope_declares(scope, name, before)
        if found is not None:
            return found
        scope = scope.parent
    return None


def _scope_declares(scope: TSNode, name: str, before: tuple[int, int]) -> str | None:
    for child in _descendants(scope, {"parameter", "property_declaration"}):
        if child.type == "property_declaration" and child.start_point >= before:
            continue  # a local must lexically precede the use
        if _binding_name(child) != name:
            continue
        # A parameter carries its type as a direct ``user_type``; a property carries it under a
        # ``type_annotation``. ``let x = expr`` (no annotation) is inferred -> no declared type.
        annotation = next((c for c in child.children if c.type == "type_annotation"), None)
        ut = next((c for c in (annotation or child).children if c.type == "user_type"), None)
        if ut is None:
            return None
        ids = [c for c in ut.children if c.type == "type_identifier"]
        return _text(ids[-1]) if ids else None
    return None


def _binding_name(node: TSNode) -> str | None:
    """The bound variable name of a parameter or property (the name precedes any ``: Type``)."""
    ident = next((c for c in node.children if c.type == "simple_identifier"), None)
    if ident is not None:
        return _text(ident)
    pattern = next((c for c in node.children if c.type == "pattern"), None)
    if pattern is not None:
        inner = next((c for c in pattern.children if c.type == "simple_identifier"), None)
        return _text(inner) if inner is not None else _text(pattern)
    return None


def _descendants(node: TSNode, types: frozenset[str] | set[str]) -> list[TSNode]:
    out: list[TSNode] = []
    stack = list(node.children)
    while stack:
        cur = stack.pop()
        if cur.type in types:
            out.append(cur)
        else:
            stack.extend(cur.children)
    return out


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_call_name(call: TSNode, node: TSNode) -> bool:
    callee = next((c for c in call.children if c.is_named), None)
    if callee is None:
        return False
    if callee.type == "simple_identifier":
        return callee.start_point == node.start_point
    if callee.type == "navigation_expression":
        suffix = next((c for c in reversed(callee.children) if c.type == "navigation_suffix"), None)
        if suffix is None:
            return False
        member = next((c for c in suffix.children if c.type == "simple_identifier"), None)
        return member is not None and member.start_point == node.start_point
    return False
