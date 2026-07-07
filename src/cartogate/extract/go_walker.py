"""Tree-sitter AST walk for Go → raw structural facts (F-08).

Deterministic and model-free, mirroring the Python/TypeScript/Java walkers. It extracts the
three top-level Go symbol kinds — ``func`` (free function), ``type`` (struct/interface/alias),
and methods ``func (r *User) Save()`` whose container is the **receiver type** — plus the name
occurrences a resolver must bind (imports, calls, struct/interface embedding, type usages).

Go specifics:
- A file's module is its **package** (the directory), supplied as ``module_qname``; a top-level
  func/type's container is that package (``is_top_level`` → gate-eligible), a method's container
  is ``package.ReceiverType`` (excluded from the gate, correctly).
- Visibility is by **capitalization**: exported (Capitalized) → PUBLIC, else INTERNAL.
- A method's signature is emitted **without its receiver** so normalization is clean.
- "Inheritance" is **embedding**: a struct/interface field with no name → an ``inherits`` edge.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_go as tsgo
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

#: The compiled Go grammar, constructed once and shared by all walkers.
_GO_LANGUAGE = Language(tsgo.language())


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "func"


class GoWalker:
    """Walks Go source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_GO_LANGUAGE)

    def walk(self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str) -> FileFacts:
        facts = FileFacts(module_qname=module_qname, rel_path=rel_path, abs_path=abs_path)
        tree = self._parser.parse(source)
        excluded: set[tuple[int, int]] = set()
        self._visit(tree.root_node, [_Frame(module_qname, "module")], facts, excluded)
        return facts

    # --- traversal ---------------------------------------------------------- #

    def _visit(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        node_type = node.type
        if node_type == "function_declaration":
            self._func(node, stack, facts, excluded, container=stack[0].qname)
            return
        if node_type == "method_declaration":
            self._method(node, stack, facts, excluded)
            return
        if node_type == "type_spec":
            self._type_spec(node, stack, facts, excluded)
            return
        if node_type == "import_spec":
            self._import(node, stack[0].qname, facts, excluded)
            return
        if node_type == "call_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type in ("type_identifier", "qualified_type"):
            self._type_reference(node, stack, facts, excluded)
            return
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _func(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]],
        *, container: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        qname = f"{container}.{name}" if container else name
        params = node.child_by_field_name("parameters")
        signature = f"{name}{_text(params) if params is not None else '()'}"
        if params is not None:
            _exclude_param_names(params, excluded)
        facts.symbols.append(_symbol(name, qname, container, signature, node))
        stack.append(_Frame(qname, "func"))
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _method(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        receiver = node.child_by_field_name("receiver")
        recv_type = _receiver_type(receiver) if receiver is not None else None
        if receiver is not None:
            _exclude_param_names(receiver, excluded)
        container = f"{stack[0].qname}.{recv_type}" if recv_type else stack[0].qname
        self._func(node, stack, facts, excluded, container=container)

    def _type_spec(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[0].qname
        qname = f"{container}.{name}" if container else name
        facts.symbols.append(_symbol(name, qname, container, name, node))
        # Struct/interface embedding (a field/elem with no field name) → an inherits edge.
        for type_id in _embedded_types(node):
            excluded.add(_point(type_id))
            facts.names.append(RawName(NAME_INHERIT, qname, *_point(type_id), _text(type_id)))
        # Still descend so nested type references (field types) are captured as references.
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _import(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        path = _strip_quotes(_text(path_node))
        alias_node = node.child_by_field_name("name")
        anchor = alias_node if alias_node is not None else path_node
        excluded.add(_point(anchor))
        local = _text(alias_node) if alias_node is not None else path.rsplit("/", 1)[-1]
        facts.names.append(RawName(NAME_IMPORT, module_qname, *_point(anchor), local, path))

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        enclosing = _enclosing(stack)
        fn = node.child_by_field_name("function")
        if fn is not None:
            target = fn.child_by_field_name("field") if fn.type == "selector_expression" else fn
            if target is not None and target.type in ("identifier", "field_identifier"):
                excluded.add(_point(target))
                facts.names.append(RawName(NAME_CALL, enclosing, *_point(target), _text(target)))
        args = node.child_by_field_name("arguments")
        if args is not None:
            self._visit(args, stack, facts, excluded)

    def _type_reference(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        type_id = node.child_by_field_name("name") if node.type == "qualified_type" else node
        if type_id is None:
            return
        point = _point(type_id)
        if point in excluded:
            return
        excluded.add(point)
        facts.names.append(RawName(NAME_REFERENCE, _enclosing(stack), *point, _text(type_id)))


# --- helpers ---------------------------------------------------------------- #


def _symbol(name: str, qname: str, container: str, signature: str, node: TSNode) -> RawSymbol:
    return RawSymbol(
        kind=NodeKind.SYMBOL,
        name=name,
        qualified_name=qname,
        container_qname=container,
        signature=signature,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        visibility=Visibility.PUBLIC if name[:1].isupper() else Visibility.INTERNAL,
        body_hash=body_hash(node.child_by_field_name("body")),
    )


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind == "func":
            return frame.qname
    return stack[0].qname


def _receiver_type(receiver: TSNode) -> str | None:
    """The receiver's type name, e.g. ``(u *User)`` -> ``User`` (pointer star dropped)."""
    for decl in receiver.children:
        if decl.type == "parameter_declaration":
            type_node = decl.child_by_field_name("type")
            if type_node is not None:
                ident = _first_type_identifier(type_node)
                return _text(ident) if ident is not None else None
    return None


def _embedded_types(type_spec: TSNode) -> list[TSNode]:
    """Embedded type identifiers (struct/interface fields/elems with no field name)."""
    out: list[TSNode] = []
    kinds = ("struct_type", "interface_type")
    body = next((c for c in type_spec.children if c.type in kinds), None)
    if body is None:
        return out
    for container in body.children:
        if container.type not in ("field_declaration_list", "interface_type"):
            continue
        for member in container.children:
            # An embedded type is a struct field with no name, or an interface ``type_elem``.
            embedded_struct = (
                member.type == "field_declaration" and member.child_by_field_name("name") is None
            )
            if not (embedded_struct or member.type == "type_elem"):
                continue
            ident = _first_type_identifier(member)
            if ident is not None:
                out.append(ident)
    return out


def _first_type_identifier(node: TSNode) -> TSNode | None:
    if node.type == "type_identifier":
        return node
    stack = list(node.children)
    while stack:
        cur = stack.pop(0)
        if cur.type == "type_identifier":
            return cur
        stack[:0] = list(cur.children)
    return None


def _exclude_param_names(params: TSNode, excluded: set[tuple[int, int]]) -> None:
    """Exclude the *name* identifiers of a parameter list (types are handled separately)."""
    for decl in params.children:
        if decl.type == "parameter_declaration":
            for child in decl.children:
                if child.type == "identifier":
                    excluded.add(_point(child))


def _strip_quotes(text: str) -> str:
    return text.strip().strip('"`')
