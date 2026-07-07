"""Tree-sitter AST walk for Kotlin → raw structural facts (F-08).

Kotlin is a JVM language whose package is declared in-source (like C#'s namespace) but which also
allows file-level functions (like C/Go), so this walker mirrors the C# walker:

- **classes / objects / interfaces** are top-level (or nested) type symbols; their **functions**
  are nested method symbols. **Top-level functions** are module-level symbols.
- a ``package`` header is **transparent** to qnames here (qnames stay file-module-based); the
  *resolver* reads the declared package + ``import`` to bind cross-package references.
- name occurrences a resolver binds: ``import``, supertypes (inherits — both base class and
  interfaces), ``Type(...)`` constructor / function calls, ``obj.method()`` / ``this.method()``
  calls, and types used in parameter / property / return positions.

A Kotlin ``.kt`` file is its own module. Constructor calls look like plain calls (``User(x)`` — no
``new``); the resolver decides type-vs-function. Extension functions, inferred receivers, and
overloaded targets are ceilings — left unresolved (sound, never a wrong edge).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_kotlin as tskotlin
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

#: The compiled Kotlin grammar, constructed once and shared by all walkers.
_KOTLIN_LANGUAGE = Language(tskotlin.language())

#: Declarations that introduce a type symbol (the grammar uses ``class_declaration`` for an
#: ``interface`` too, distinguished by a keyword child — both are types here).
_TYPE_DECLS = frozenset({"class_declaration", "object_declaration"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "type" | "function"


class KotlinWalker:
    """Walks Kotlin source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_KOTLIN_LANGUAGE)

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
        if node_type == "import":
            self._import(node, stack[0].qname, facts, excluded)
            return
        if node_type in _TYPE_DECLS:
            self._type_decl(node, stack, facts, excluded)
            return
        if node_type == "function_declaration":
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
        qid = next((c for c in node.children if c.type == "qualified_identifier"), None)
        if qid is None:
            return
        fqn = _text(qid)
        simple = fqn.rsplit(".", 1)[-1]
        tail = _last_identifier(qid)
        if tail is not None:
            excluded.add(_point(tail))
        facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(qid), simple, fqn))

    def _type_decl(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name
        self._supertypes(node, qname, facts, excluded)
        facts.symbols.append(_symbol(name, qname, container, name, node, _visibility(node)))
        stack.append(_Frame(qname, "type"))
        ctor = next((c for c in node.children if c.type == "primary_constructor"), None)
        if ctor is not None:
            self._visit(ctor, stack, facts, excluded)  # class-parameter types are references
        body = next((c for c in node.children if c.type in ("class_body", "enum_class_body")), None)
        if body is not None:
            for child in body.children:
                self._visit(child, stack, facts, excluded)
        stack.pop()

    def _supertypes(
        self, node: TSNode, type_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        specs = next((c for c in node.children if c.type == "delegation_specifiers"), None)
        if specs is None:
            return
        for spec in specs.children:
            if spec.type != "delegation_specifier":
                continue
            type_id = _user_type_identifier(spec)
            if type_id is not None:
                excluded.add(_point(type_id))
                facts.names.append(RawName(NAME_INHERIT, type_qname, *_point(type_id),
                                           _text(type_id)))

    def _function(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name
        params = next((c for c in node.children if c.type == "function_value_parameters"), None)
        signature = f"{name}{_text(params) if params is not None else '()'}"
        body = next((c for c in node.children if c.type == "function_body"), None)
        facts.symbols.append(_symbol(name, qname, container, signature, node, _visibility(node),
                                     body_hash(body)))
        stack.append(_Frame(qname, "function"))
        if params is not None:
            self._visit(params, stack, facts, excluded)
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        callee = next((c for c in node.children if c.is_named), None)
        name_node: TSNode | None = None
        receiver: TSNode | None = None
        if callee is not None and callee.type == "identifier":
            name_node = callee  # ``f(...)`` / ``User(...)`` — function or constructor
        elif callee is not None and callee.type == "navigation_expression":
            name_node = _navigation_member(callee)
            receiver = _navigation_receiver(callee)
        if name_node is not None:
            excluded.add(_point(name_node))
            facts.names.append(RawName(NAME_CALL, _enclosing(stack), *_point(name_node),
                                       _text(name_node)))
        if receiver is not None:
            self._visit(receiver, stack, facts, excluded)
        args = next((c for c in node.children if c.type == "value_arguments"), None)
        if args is not None:
            self._visit(args, stack, facts, excluded)

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
    """Map Kotlin visibility modifiers to Cartogate visibility (default ``public`` → PUBLIC)."""
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is not None:
        names = {_text(m) for m in mods.children if m.type == "visibility_modifier"}
        if "private" in names or "protected" in names:
            return Visibility.INTERNAL
        if "internal" in names:
            return Visibility.EXPORTED
    return Visibility.PUBLIC


def _user_type_identifier(node: TSNode) -> TSNode | None:
    """The salient ``identifier`` naming the type under a ``user_type`` / ``delegation_specifier`` /
    ``constructor_invocation`` (``a.b.User`` -> ``User``), or ``None``."""
    user_type = node if node.type == "user_type" else next(
        (d for d in _descendants(node, {"user_type"})), None
    )
    if user_type is None:
        return None
    ids = [c for c in user_type.children if c.type in ("identifier", "type_identifier")]
    return ids[-1] if ids else None


def _navigation_member(nav: TSNode) -> TSNode | None:
    """The member identifier of a ``navigation_expression`` (``a.b.c`` -> the trailing ``c``)."""
    ids = [c for c in nav.children if c.type == "identifier"]
    return ids[-1] if ids else None


def _navigation_receiver(nav: TSNode) -> TSNode | None:
    """The receiver expression of a ``navigation_expression`` (the first child)."""
    return next((c for c in nav.children if c.is_named), None)


def _last_identifier(node: TSNode) -> TSNode | None:
    ids = [c for c in node.children if c.type == "identifier"]
    return ids[-1] if ids else None


def _descendants(node: TSNode, types: frozenset[str] | set[str]) -> list[TSNode]:
    out: list[TSNode] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in types:
            out.append(cur)
        else:
            stack.extend(cur.children)
    return out


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind in ("type", "function"):
            return frame.qname
    return stack[0].qname
