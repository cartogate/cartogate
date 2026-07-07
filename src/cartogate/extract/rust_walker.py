"""Tree-sitter AST walk for Rust → raw structural facts (F-08).

Deterministic and model-free, mirroring the other walkers. It extracts Rust's top-level symbol
kinds — ``fn`` (free function), ``struct``/``enum``/``union``/``trait``/``type`` (types), and
methods inside ``impl`` blocks (container = the impl's type) — plus the name occurrences a
resolver must bind (``use`` imports, calls, ``impl Trait for Type`` implementations, type usages).

Rust specifics:
- A file *is* a module (``file_is_namespace=True``); ``mod foo.rs``/``foo/mod.rs`` → module ``foo``,
  ``lib.rs``/``main.rs`` → the crate root. An inline ``mod x { … }`` nests via a scope frame.
- Free functions and types are top-level (gate-eligible, Python-like); ``impl`` methods are nested
  under their type, so the gate excludes them via ``is_top_level``.
- Visibility is by ``pub`` (``pub`` → PUBLIC, ``pub(crate)``/``pub(super)`` → EXPORTED, none →
  INTERNAL). A method's signature is emitted without its ``self`` receiver.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_rust as tsrust
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

#: The compiled Rust grammar, constructed once and shared by all walkers.
_RUST_LANGUAGE = Language(tsrust.language())

#: Declarations that introduce a top-level/nested *type* symbol.
_TYPE_DECLS = frozenset({"struct_item", "enum_item", "union_item", "trait_item", "type_item"})
#: Function declarations (free fns + impl/trait methods).
_FN_DECLS = frozenset({"function_item", "function_signature_item"})


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "fn"


class RustWalker:
    """Walks Rust source into :class:`FileFacts`. One instance is reused across files."""

    def __init__(self) -> None:
        self._parser = Parser(_RUST_LANGUAGE)

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
        if node_type in _FN_DECLS:
            self._fn(node, stack[-1].qname, stack, facts, excluded)
            return
        if node_type in _TYPE_DECLS:
            self._type(node, stack, facts, excluded)
            return
        if node_type == "impl_item":
            self._impl(node, stack, facts, excluded)
            return
        if node_type == "mod_item":
            self._mod(node, stack, facts, excluded)
            return
        if node_type == "use_declaration":
            self._use(node, stack[0].qname, facts, excluded)
            return
        if node_type == "call_expression":
            self._call(node, stack, facts, excluded)
            return
        if node_type == "type_identifier":
            self._type_reference(node, stack, facts, excluded)
            return
        for child in node.children:
            self._visit(child, stack, facts, excluded)

    def _fn(
        self, node: TSNode, container: str, stack: list[_Frame], facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        qname = f"{container}.{name}" if container else name
        params = node.child_by_field_name("parameters")
        signature = f"{name}{_text(params) if params is not None else '()'}"
        facts.symbols.append(_symbol(name, qname, container, signature, node))
        stack.append(_Frame(qname, "fn"))
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit(body, stack, facts, excluded)
        stack.pop()

    def _type(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}" if container else name
        facts.symbols.append(_symbol(name, qname, container, name, node))
        # Descend so field/variant types become references and supertrait bounds become inherits.
        # Push the type as a frame so a trait's method *signatures* nest under it
        # (``Greeter.greet``, not a free ``greet`` in the module) — mirroring impl methods.
        stack.append(_Frame(qname, "module"))
        for child in node.children:
            self._visit(child, stack, facts, excluded)
        stack.pop()

    def _impl(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        module = stack[0].qname
        impl_type = _text(type_node) if type_node is not None else ""
        container = f"{module}.{impl_type}" if module else impl_type
        if type_node is not None:
            excluded.add(_point(type_node))
        # `impl Trait for Type` → an `implements`-style inherits edge from the type to the trait.
        if trait_node is not None and type_node is not None:
            excluded.add(_point(trait_node))
            facts.names.append(
                RawName(NAME_INHERIT, container, *_point(trait_node), _text(trait_node))
            )
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                if child.type in _FN_DECLS:
                    self._fn(child, container, stack, facts, excluded)
                else:
                    self._visit(child, stack, facts, excluded)

    def _mod(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:  # `mod foo;` (external file) has no inline body
            return
        excluded.add(_point(name_node))
        module = stack[0].qname
        qname = f"{module}.{_text(name_node)}" if module else _text(name_node)
        stack.append(_Frame(qname, "module"))
        for child in body.children:
            self._visit(child, stack, facts, excluded)
        stack.pop()

    def _use(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        for path, anchor in _use_paths(node):
            excluded.add(_point(anchor))
            facts.names.append(
                RawName(NAME_IMPORT, module_qname, *_point(anchor), path.rsplit("::", 1)[-1], path)
            )

    def _call(
        self, node: TSNode, stack: list[_Frame], facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        fn = node.child_by_field_name("function")
        if fn is not None:
            target = _call_target(fn)
            if target is not None:
                excluded.add(_point(target))
                facts.names.append(
                    RawName(NAME_CALL, _enclosing(stack), *_point(target), _text(target))
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


def _symbol(name: str, qname: str, container: str, signature: str, node: TSNode) -> RawSymbol:
    return RawSymbol(
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


def _visibility(node: TSNode) -> Visibility:
    vis = next((c for c in node.children if c.type == "visibility_modifier"), None)
    if vis is None:
        return Visibility.INTERNAL
    return Visibility.PUBLIC if _text(vis) == "pub" else Visibility.EXPORTED  # pub(crate)/(super)


def _enclosing(stack: list[_Frame]) -> str:
    for frame in reversed(stack):
        if frame.kind == "fn":
            return frame.qname
    return stack[0].qname


def _call_target(fn: TSNode) -> TSNode | None:
    """The identifier a call binds to: ``foo`` / the ``name`` of ``a::b::foo`` or ``x.method``."""
    if fn.type == "identifier":
        return fn
    if fn.type in ("scoped_identifier", "field_expression"):
        return fn.child_by_field_name("name") or fn.child_by_field_name("field")
    return None


def _use_paths(node: TSNode) -> list[tuple[str, TSNode]]:
    """Flatten a ``use`` tree into ``(full_path, anchor_node)`` pairs (handles ``{A, B}`` lists)."""
    out: list[tuple[str, TSNode]] = []

    def walk(arg: TSNode, prefix: str) -> None:
        if arg.type == "scoped_identifier":
            out.append((_join(prefix, _text(arg)), arg.child_by_field_name("name") or arg))
        elif arg.type == "identifier":
            out.append((_join(prefix, _text(arg)), arg))
        elif arg.type == "use_as_clause":
            path = arg.child_by_field_name("path")
            if path is not None:
                out.append((_join(prefix, _text(path)), path))
        elif arg.type == "scoped_use_list":
            base = arg.child_by_field_name("path")
            base_path = _join(prefix, _text(base)) if base is not None else prefix
            lst = arg.child_by_field_name("list")
            if lst is not None:
                for item in lst.named_children:
                    walk(item, base_path)
        elif arg.type == "use_list":
            for item in arg.named_children:
                walk(item, prefix)

    arg = node.child_by_field_name("argument")
    if arg is not None:
        walk(arg, "")
    return out


def _join(prefix: str, suffix: str) -> str:
    return f"{prefix}::{suffix}" if prefix else suffix
