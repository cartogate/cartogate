"""Tree-sitter AST walk for Swift → raw structural facts (F-08).

Swift has a flat module namespace (no in-source ``package``/``namespace`` — a module is the build
unit), so qnames are file-module-based and types resolve repo-wide by name. The grammar reuses
``class_declaration`` for ``class``/``struct``/``enum``/``extension`` (distinguished by a keyword).

- **classes / structs / enums / protocols** are top-level type symbols; their **functions** are
  nested method symbols; an ``extension Type {}`` adds methods to ``Type``.
- **top-level functions** are module-level symbols.
- name occurrences a resolver binds: ``import`` (external module), supertypes (inherits — base
  class + protocols), ``Type(...)`` initializer / function calls, ``obj.m()`` / ``self.m()`` calls,
  and types used in parameter / property / return positions.

A ``.swift`` file is its own module. Initializer calls look like plain calls (``User(name:)`` — no
``new``); the resolver decides type-vs-function. Inferred receivers, protocol-witness dispatch, and
overloaded targets are ceilings — left unresolved (sound, never a wrong edge).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_swift as tsswift
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

#: The compiled Swift grammar, constructed once and shared by all walkers.
_SWIFT_LANGUAGE = Language(tsswift.language())


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "type" | "function"


class SwiftWalker:
    """Walks Swift source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_SWIFT_LANGUAGE)

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
        if node_type == "import_declaration":
            self._import(node, stack[0].qname, facts, excluded)
            return
        if node_type in ("class_declaration", "protocol_declaration"):
            self._type_decl(node, stack, facts, excluded)
            return
        if node_type in ("function_declaration", "init_declaration"):
            self._function(node, stack, facts, excluded)
            return
        if node_type == "call_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "user_type":
            self._type_reference(node, stack, facts, excluded)
            return
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _import(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        ident = _last_simple_identifier(node)
        if ident is None:
            return
        excluded.add(_point(ident))
        name = _text(ident)
        facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(ident), name, name))

    def _type_decl(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        # ``extension Type {}`` has no ``type_identifier`` name — its target is a ``user_type``;
        # its members attach to that existing type. A normal type is named by a ``type_identifier``.
        is_extension = any(not c.is_named and _text(c) == "extension" for c in node.children)
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "type_identifier"), None
        )
        container = stack[-1].qname
        if is_extension or name_node is None:
            target = _user_type_identifier(node)  # the extended type
            if target is None:
                return
            name = _text(target)
            qname = f"{container}.{name}" if container else name
            excluded.add(_point(target))
            # No new type symbol for an extension; just descend with the type as the scope.
        else:
            excluded.add(_point(name_node))
            name = _text(name_node)
            qname = f"{container}.{name}" if container else name
            self._supertypes(node, qname, facts, excluded)
            facts.symbols.append(_symbol(name, qname, container, name, node, _visibility(node)))
        stack.append(_Frame(qname, "type"))
        body = next((c for c in node.children if c.type in ("class_body", "protocol_body",
                                                            "enum_class_body")), None)
        if body is not None:
            for child in body.children:
                self._visit(child, stack, facts, excluded)
        stack.pop()

    def _supertypes(
        self, node: TSNode, type_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        for spec in node.children:
            if spec.type != "inheritance_specifier":
                continue
            type_id = _user_type_identifier(spec)
            if type_id is not None:
                excluded.add(_point(type_id))
                facts.names.append(RawName(NAME_INHERIT, type_qname, *_point(type_id),
                                           _text(type_id)))

    def _function(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        if node.type == "init_declaration":
            name = "init"
        else:
            name_node = next((c for c in node.children if c.type == "simple_identifier"), None)
            if name_node is None:
                return
            excluded.add(_point(name_node))
            name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name
        params = [c for c in node.children if c.type == "parameter"]
        signature = f"{name}({', '.join(_text(p) for p in params)})"
        body = next((c for c in node.children if c.type == "function_body"), None)
        facts.symbols.append(_symbol(name, qname, container, signature, node, _visibility(node),
                                     body_hash(body)))
        stack.append(_Frame(qname, "function"))
        for param in params:
            self._visit(param, stack, facts, excluded)
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        callee = next((c for c in node.children if c.is_named), None)
        name_node: TSNode | None = None
        receiver: TSNode | None = None
        if callee is not None and callee.type == "simple_identifier":
            name_node = callee  # ``f(...)`` / ``User(...)`` — function or initializer
        elif callee is not None and callee.type == "navigation_expression":
            name_node = _navigation_member(callee)
            receiver = _navigation_receiver(callee)
        if name_node is not None:
            excluded.add(_point(name_node))
            facts.names.append(RawName(NAME_CALL, _enclosing(stack), *_point(name_node),
                                       _text(name_node)))
        if receiver is not None:
            self._visit(receiver, stack, facts, excluded)
        suffix = next((c for c in node.children if c.type == "call_suffix"), None)
        if suffix is not None:
            self._visit(suffix, stack, facts, excluded)

    def _type_reference(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        type_id = _user_type_identifier(node)
        if type_id is None:
            return
        point = _point(type_id)
        if point in excluded:
            return
        excluded.add(point)
        facts.names.append(RawName(NAME_REFERENCE, _enclosing(stack), *point, _text(type_id)))


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


def _visibility(node: TSNode) -> Visibility:
    """Map Swift visibility modifiers to Cartogate visibility (default ``internal`` → EXPORTED)."""
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is not None:
        names = {_text(m) for m in mods.children if m.type == "visibility_modifier"}
        if "public" in names or "open" in names:
            return Visibility.PUBLIC
        if "private" in names or "fileprivate" in names:
            return Visibility.INTERNAL
    return Visibility.EXPORTED  # Swift's default ``internal`` = module-visible


def _user_type_identifier(node: TSNode) -> TSNode | None:
    """The salient ``type_identifier`` of a ``user_type`` (``a.b.User`` -> ``User``), looking under
    ``node`` for the first ``user_type``."""
    user_type = node if node.type == "user_type" else next(
        iter(_descendants(node, {"user_type"})), None
    )
    if user_type is None:
        return None
    ids = [c for c in user_type.children if c.type == "type_identifier"]
    return ids[-1] if ids else None


def _navigation_member(nav: TSNode) -> TSNode | None:
    """The member identifier of a ``navigation_expression`` (the ``simple_identifier`` in the
    trailing ``navigation_suffix``)."""
    suffix = next((c for c in reversed(nav.children) if c.type == "navigation_suffix"), None)
    if suffix is None:
        return None
    return next((c for c in suffix.children if c.type == "simple_identifier"), None)


def _navigation_receiver(nav: TSNode) -> TSNode | None:
    return next((c for c in nav.children if c.is_named and c.type != "navigation_suffix"), None)


def _last_simple_identifier(node: TSNode) -> TSNode | None:
    ids = _descendants(node, {"simple_identifier"})
    return ids[-1] if ids else None


def _descendants(node: TSNode, types: frozenset[str] | set[str]) -> list[TSNode]:
    out: list[TSNode] = []
    stack = list(node.children)
    while stack:
        cur = stack.pop(0)
        if cur.type in types:
            out.append(cur)
        else:
            stack[:0] = cur.children
    return out


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind in ("type", "function"):
            return frame.qname
    return stack[0].qname
