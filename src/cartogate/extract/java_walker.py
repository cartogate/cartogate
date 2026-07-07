"""Tree-sitter AST walk for Java → raw structural facts (F-08).

Deterministic and model-free, mirroring ``ast_walker`` (Python) and ``ts_walker`` (TypeScript):
it extracts type/method definitions with scope-derived qualified names and the *positions* of
the names a resolver must later bind (calls, ``new`` targets, imports, ``extends``/``implements``
bases, and type usages). Binding is the resolver's job (``resolver_java``); this never guesses.

Java specifics:
- A file's module is its **package** (the directory), supplied as ``module_qname`` by the
  pipeline; a top-level type's container is therefore that package, and its qname is
  ``package.Type`` (clean, no redundant file segment).
- Methods/constructors are nested symbols (container = the enclosing type), so the duplicate
  gate's ``is_top_level`` rule excludes them automatically — only top-level types are gated.
- Overloaded methods share a qname but differ by parameter type, so the pipeline keeps them as
  distinct nodes (by-type signature + a per-signature ``stmt_ordinal``); an exact re-declaration
  still collapses. A call to an overloaded method is left unresolved (no arg-type inference).
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_java as tsjava
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

#: The compiled Java grammar, constructed once and shared by all walkers.
_JAVA_LANGUAGE = Language(tsjava.language())

#: Declarations that introduce a top-level/nested *type* symbol.
_TYPE_DECLS = frozenset({
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
})
#: Declarations that introduce a *member callable* symbol.
_CALLABLE_DECLS = frozenset({"method_declaration", "constructor_declaration"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "type" | "callable"


class JavaWalker:
    """Walks Java source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_JAVA_LANGUAGE)

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
        if node_type == "import_declaration":
            self._import(node, stack[0].qname, facts, excluded)
            return
        if node_type == "method_invocation":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "object_creation_expression":
            self._new(node, stack, facts, excluded)
            return
        if node_type == "type_identifier":
            self._type_reference(node, stack, facts, excluded)
            return
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

        # A type's gate signature is its bare name: you cannot have two top-level types of the
        # same name in a package, so re-declaring ``class User`` is a duplicate regardless of its
        # bases. Bases are still recorded as ``inherits`` edges below.
        self._record_bases(node, qname, facts, excluded, [])

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
        if params is not None:
            _exclude_param_names(params, excluded)

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
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _record_bases(
        self,
        node: TSNode,
        type_qname: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        out: list[str],
    ) -> None:
        for child in node.children:
            if child.type in ("superclass", "super_interfaces", "extends_interfaces"):
                for type_id in _type_identifiers(child):
                    excluded.add(_point(type_id))
                    base = _text(type_id)
                    out.append(base)
                    facts.names.append(RawName(NAME_INHERIT, type_qname, *_point(type_id), base))

    def _import(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        scoped = next((c for c in node.children if c.type in ("scoped_identifier", "identifier")),
                      None)
        if scoped is None:
            return
        fqn = _text(scoped)
        wildcard = any(c.type == "asterisk" for c in node.children)
        # The imported simple name is the FQN's last segment (a type, or a member for `static`).
        simple = fqn.rsplit(".", 1)[-1]
        target = _last_identifier(scoped)
        if target is None:
            return
        excluded.add(_point(target))
        # module carries the full FQN; text is the simple name (or "*" for a wildcard package).
        name_text = "*" if wildcard else simple
        facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(target), name_text, fqn))

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        enclosing = _enclosing(stack)
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            excluded.add(_point(name_node))
            facts.names.append(RawName(NAME_CALL, enclosing, *_point(name_node), _text(name_node)))
        for field in ("object", "arguments"):
            child = node.child_by_field_name(field)
            if child is not None:
                self._visit(child, stack, facts, excluded)

    def _new(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        enclosing = _enclosing(stack)
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            type_id = _last_type_identifier(type_node)
            if type_id is not None:
                excluded.add(_point(type_id))
                facts.names.append(
                    RawName(NAME_CALL, enclosing, *_point(type_id), _text(type_id))
                )
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


def _enclosing(stack: list[_Frame]) -> str:
    """The qname an edge originates from: the nearest type/callable scope (else the module)."""
    for frame in reversed(stack):
        if frame.kind in ("type", "callable"):
            return frame.qname
    return stack[0].qname


def _visibility(node: TSNode) -> Visibility:
    """Map Java access modifiers to Cartogate visibility (default: package-private → INTERNAL)."""
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    if mods is not None:
        names = {_text(m) for m in mods.children}
        if "public" in names:
            return Visibility.PUBLIC
        if "protected" in names:
            return Visibility.EXPORTED
    return Visibility.INTERNAL


def _type_identifiers(node: TSNode) -> list[TSNode]:
    """All ``type_identifier`` leaves under a heritage clause (extends/implements)."""
    out: list[TSNode] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "type_identifier":
            out.append(cur)
        else:
            stack.extend(cur.children)
    return out


def _last_type_identifier(node: TSNode) -> TSNode | None:
    """The final ``type_identifier`` of a (possibly generic/scoped) type (``a.b.User`` -> User)."""
    found = _type_identifiers(node)
    return found[-1] if found else (node if node.type == "type_identifier" else None)


def _last_identifier(node: TSNode) -> TSNode | None:
    """The trailing ``identifier`` of a (possibly scoped) name, e.g. ``a.b.User`` -> ``User``."""
    if node.type == "identifier":
        return node
    last: TSNode | None = None
    stack = [node]
    order: list[TSNode] = []
    while stack:
        cur = stack.pop()
        if cur.type == "identifier":
            order.append(cur)
        stack.extend(reversed(cur.children))
    # The last identifier in source order is the bound simple name.
    for ident in order:
        if last is None or ident.start_point > last.start_point:
            last = ident
    return last


def _exclude_param_names(params: TSNode, excluded: set[tuple[int, int]]) -> None:
    """Exclude the *name* identifier of each formal parameter (types are handled separately)."""
    for param in params.children:
        if param.type in ("formal_parameter", "spread_parameter"):
            name = param.child_by_field_name("name")
            if name is not None:
                excluded.add(_point(name))
