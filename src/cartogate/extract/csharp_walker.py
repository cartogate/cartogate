"""Tree-sitter AST walk for C# → raw structural facts (F-08).

Deterministic and model-free, mirroring ``java_walker``: it extracts type/method definitions with
scope-derived qualified names and the *positions* of the names a resolver must later bind (calls,
``new`` targets, ``using`` imports, ``base``-list bases, and type usages). Binding is the
resolver's job (``resolver_csharp``); this never guesses.

C# specifics:
- A ``.cs`` file is its **own module** (``file_is_namespace=True``, like Python/TS/Rust): a type's
  qname is ``<file-module>.Type``. An in-file ``namespace X.Y {}`` (or file-scoped ``namespace
  X.Y;``) is **transparent** to qnames here — it is the *resolver* that reads the declared
  namespace + ``using`` directives to bind cross-namespace references (it returns a definition's
  ``(path, line)``, which the pipeline maps to the node, so the namespace scheme stays internal to
  resolution and never has to match a node's qname).
- Methods/constructors are nested symbols (container = the enclosing type), so the duplicate gate's
  ``is_top_level`` rule excludes them automatically — only top-level types are gated.
- Overloaded methods share a qname but differ by parameter type, so the pipeline keeps them as
  distinct nodes (by-type signature + a per-signature ``stmt_ordinal``); an exact re-declaration
  still collapses. A call to an overloaded method is left unresolved (no arg-type inference).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_c_sharp as tscsharp
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

#: The compiled C# grammar, constructed once and shared by all walkers.
_CSHARP_LANGUAGE = Language(tscsharp.language())

#: Declarations that introduce a top-level/nested *type* symbol.
_TYPE_DECLS = frozenset({
    "class_declaration",
    "interface_declaration",
    "struct_declaration",
    "enum_declaration",
    "record_declaration",
    "record_struct_declaration",
})
#: Declarations that introduce a *member callable* symbol.
_CALLABLE_DECLS = frozenset({"method_declaration", "constructor_declaration"})
#: Namespace wrappers we descend through transparently (they don't add to the file-based qname).
_NAMESPACE_DECLS = frozenset({"namespace_declaration", "file_scoped_namespace_declaration"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "type" | "callable"


class CSharpWalker:
    """Walks C# source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_CSHARP_LANGUAGE)

    def walk(self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str) -> FileFacts:
        facts = FileFacts(module_qname=module_qname, rel_path=rel_path, abs_path=abs_path)
        tree = self._parser.parse(source)
        excluded: set[tuple[int, int]] = set()
        self._visit(tree.root_node, [_Frame(module_qname, "module")], facts, excluded)
        return facts

    # --- traversal ---------------------------------------------------------- #

    def _visit(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        node_type = node.type
        if node_type in _TYPE_DECLS:
            self._type_decl(node, stack, facts, excluded)
            return
        if node_type in _CALLABLE_DECLS:
            self._callable_decl(node, stack, facts, excluded)
            return
        if node_type == "using_directive":
            self._using(node, stack[0].qname, facts, excluded)
            return
        if node_type == "invocation_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "object_creation_expression":
            self._new(node, stack, facts, excluded)
            return
        # A namespace wrapper / anything else: descend (the namespace does not change the qname).
        for child in node.children:
            self._visit(child, stack, facts, excluded)

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

        # A type's gate signature is its bare name: you cannot have two top-level types of the same
        # name in a namespace, so re-declaring ``class User`` is a duplicate regardless of its
        # bases. Bases are still recorded as ``inherits`` edges below.
        self._record_bases(node, qname, facts, excluded)

        facts.symbols.append(
            RawSymbol(
                kind=NodeKind.SYMBOL,
                name=name,
                qualified_name=qname,
                container_qname=container,
                signature=name,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                visibility=_visibility(node),
            )
        )
        stack.append(_Frame(qname, "type"))
        # A positional record's parameters (``record Pair(int A, int B)``) carry types worth a ref.
        params = node.child_by_field_name("parameters")
        if params is not None:
            self._param_types(params, stack, facts, excluded)
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                self._visit(child, stack, facts, excluded)
        stack.pop()

    def _callable_decl(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name

        params = node.child_by_field_name("parameters")
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
                visibility=_visibility(node),
                body_hash=body_hash(node.child_by_field_name("body")),
            )
        )
        stack.append(_Frame(qname, "callable"))
        # Parameter types are type references (the names are excluded so they don't read as refs).
        if params is not None:
            self._param_types(params, stack, facts, excluded)
        # The return type (the type child sitting before the name) is a type reference too.
        self._return_type_ref(node, name_node, stack, facts, excluded)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _record_bases(
        self, node: TSNode, type_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        base_list = next((c for c in node.children if c.type == "base_list"), None)
        if base_list is None:
            return
        for type_id in _base_type_identifiers(base_list):
            excluded.add(_point(type_id))
            facts.names.append(RawName(NAME_INHERIT, type_qname, *_point(type_id), _text(type_id)))

    def _using(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        # ``using System;`` / ``using App.Models;`` / ``using Alias = A.B;`` / ``using static A.B;``
        # — bind the namespace (or aliased target). Skip the alias identifier and ``static`` kw.
        alias_node = node.child_by_field_name("name")
        name = next(
            (c for c in node.children
             if c.type in ("qualified_name", "identifier")
             and (alias_node is None or c.id != alias_node.id)),
            None,
        )
        if name is None:
            return
        target = _last_identifier(name)
        if target is None:
            return
        excluded.add(_point(target))
        facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(target), _text(target),
                                   _text(name)))

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        enclosing = _enclosing(stack)
        func = node.child_by_field_name("function")
        name_node: TSNode | None = None
        receiver: TSNode | None = None
        if func is not None and func.type == "identifier":
            name_node = func  # unqualified call: ``Validate(x)``
        elif func is not None and func.type == "member_access_expression":
            name_node = func.child_by_field_name("name")  # ``recv.Method(...)``
            receiver = func.child_by_field_name("expression")
        if name_node is not None:
            excluded.add(_point(name_node))
            facts.names.append(RawName(NAME_CALL, enclosing, *_point(name_node), _text(name_node)))
        if receiver is not None:
            self._visit(receiver, stack, facts, excluded)
        args = node.child_by_field_name("arguments")
        if args is not None:
            self._visit(args, stack, facts, excluded)

    def _new(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        enclosing = _enclosing(stack)
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            type_id = _last_identifier(type_node)
            if type_id is not None:
                excluded.add(_point(type_id))
                facts.names.append(RawName(NAME_CALL, enclosing, *_point(type_id), _text(type_id)))
        args = node.child_by_field_name("arguments")
        if args is not None:
            self._visit(args, stack, facts, excluded)

    # --- type references ---------------------------------------------------- #

    def _param_types(
        self, params: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        for param in params.children:
            if param.type != "parameter":
                continue
            name_node = param.child_by_field_name("name")
            if name_node is not None:
                excluded.add(_point(name_node))
            self._type_ref(param.child_by_field_name("type"), stack, facts, excluded)

    def _return_type_ref(
        self,
        node: TSNode,
        name_node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        # The return type is the named-type child positioned before the method name.
        for child in node.children:
            if child.start_point >= name_node.start_point:
                break
            if child.type in _TYPE_NODES:
                self._type_ref(child, stack, facts, excluded)

    def _type_ref(
        self,
        type_node: TSNode | None,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        """Emit a type-usage reference on a type node's salient identifier (skip primitives)."""
        type_id = _type_identifier(type_node)
        if type_id is None:
            return
        point = _point(type_id)
        if point in excluded:
            return
        excluded.add(point)
        facts.names.append(RawName(NAME_REFERENCE, _enclosing(stack), *point, _text(type_id)))


# --- helpers ---------------------------------------------------------------- #

#: Grammar nodes that can appear in a type position.
_TYPE_NODES = frozenset({
    "identifier", "qualified_name", "generic_name", "nullable_type", "array_type",
    "predefined_type",
})


def _enclosing(stack: list[_Frame]) -> str:
    """The qname an edge originates from: the nearest type/callable scope (else the module)."""
    for frame in reversed(stack):
        if frame.kind in ("type", "callable"):
            return frame.qname
    return stack[0].qname


def _visibility(node: TSNode) -> Visibility:
    """Map C# access modifiers to Cartogate visibility (default → INTERNAL, C#'s own default)."""
    names = {_text(c) for c in node.children if c.type == "modifier"}
    if "public" in names:
        return Visibility.PUBLIC
    if "protected" in names or "internal" in names:
        return Visibility.EXPORTED
    return Visibility.INTERNAL


def _base_type_identifiers(base_list: TSNode) -> list[TSNode]:
    """The salient type identifier of each base in a ``base_list`` (``: Base, IFace``)."""
    out: list[TSNode] = []
    for child in base_list.children:
        if not child.is_named:
            continue
        type_id = _type_identifier(child)
        if type_id is not None:
            out.append(type_id)
    return out


def _type_identifier(type_node: TSNode | None) -> TSNode | None:
    """The identifier naming a (possibly generic/qualified/nullable/array) type, or ``None`` for a
    predefined primitive (``int``/``bool``/``string``) which is never an in-repo type."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "identifier":
        return type_node
    if t == "qualified_name":  # ``A.B.User`` -> ``User``
        return _last_identifier(type_node)
    if t == "generic_name":  # ``List<User>`` -> the base name ``List``
        base = type_node.child_by_field_name("name") or next(
            (c for c in type_node.children if c.type == "identifier"), None
        )
        return base
    if t in ("nullable_type", "array_type"):  # ``User?`` / ``User[]`` -> unwrap the element type
        return _type_identifier(type_node.child_by_field_name("type") or _first_named(type_node))
    return None  # predefined_type and anything else: not an in-repo type reference


def _last_identifier(node: TSNode) -> TSNode | None:
    """The trailing ``identifier`` of a (possibly qualified) name, e.g. ``A.B.User`` -> ``User``."""
    if node.type == "identifier":
        return node
    last: TSNode | None = None
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "identifier" and (last is None or cur.start_point > last.start_point):
            last = cur
        stack.extend(cur.children)
    return last


def _first_named(node: TSNode) -> TSNode | None:
    return next((c for c in node.children if c.is_named), None)
