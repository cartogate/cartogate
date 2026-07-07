"""Tree-sitter AST walk for Python → raw structural facts (spec §5.1).

This pass is deterministic and model-free. It extracts symbol definitions (functions,
classes, methods) with stable scope-derived qualified names, plus the *positions* of the
names that name resolution must later bind: call targets, base classes, imported names,
and other in-scope references. Binding those positions to concrete nodes is the resolver's
job (``resolver.py``); this module never guesses a target.

Statement-level identity (CPG-shaped) is reserved for a later phase: v0 emits symbol and
module granularity only, but the qualified-name scheme already nests cleanly so statement
ordinals can be layered on without disturbing existing ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter_python as tspython
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.treesitter_util import body_hash
from cartogate.extract.treesitter_util import point as _point
from cartogate.extract.treesitter_util import text as _text
from cartogate.schema.enums import NodeKind, Visibility

#: The compiled Python grammar, constructed once and shared by all walkers.
_PYTHON_LANGUAGE = Language(tspython.language())

# Kinds of a name reference that resolution must bind, in order of how it is emitted.
NAME_CALL = "call"
NAME_REFERENCE = "reference"
NAME_IMPORT = "import"
NAME_INHERIT = "inherit"


@dataclass(frozen=True, slots=True)
class RawSymbol:
    """A symbol definition discovered structurally (function / class / method)."""

    kind: NodeKind
    name: str
    qualified_name: str
    container_qname: str  # enclosing scope — the src of the ``defines`` edge
    signature: str
    start_line: int  # 1-based; equals the def/class line (matches a resolver hit)
    end_line: int
    #: Explicit visibility when the language carries it (TypeScript ``export``/``private``);
    #: ``None`` lets the pipeline derive it from the name (Python's ``_`` convention).
    visibility: Visibility | None = None
    #: True for a TYPE DECLARATION (class/interface/struct/trait) as opposed to a callable.
    #: Type declarations sharing a signature (name + bases) are often idiomatic (per-component
    #: React Props, per-service Settings) — the gate blocks them only on a matching body hash.
    is_type_decl: bool = False
    #: Whitespace-normalized hash of the body block, for near-duplicate detection (F-32). ``None``
    #: for a symbol whose grammar node has no ``body`` field (a module/external, a Go/Rust type
    #: spec, a TS arrow whose declarator carries no body) — those don't participate in clone
    #: grouping. Classes/enums/traits whose grammar exposes a body ARE included.
    body_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RawName:
    """A name occurrence whose target the resolver must bind to a node."""

    relation: str  # one of NAME_CALL / NAME_REFERENCE / NAME_IMPORT / NAME_INHERIT
    enclosing_qname: str  # the src of the resulting edge
    line: int  # 1-based row of the name token
    column: int  # 0-based column of the name token
    text: str  # source text of the name (fallback label for external packages)
    #: For imports, the module the name comes from — ``a.b`` for ``import a.b``, the
    #: ``from`` module for ``from a.b import x``. Used to name an external package by its
    #: package (``a``) rather than the imported symbol. Empty for non-imports.
    module: str = ""


@dataclass(slots=True)
class FileFacts:
    """All structural facts extracted from a single source file."""

    module_qname: str
    rel_path: str
    abs_path: str
    symbols: list[RawSymbol] = field(default_factory=list)
    names: list[RawName] = field(default_factory=list)


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "class" | "function"


class TreeSitterWalker:
    """Walks Python source into :class:`FileFacts`. Reusable across files."""

    def __init__(self) -> None:
        self._parser = Parser(_PYTHON_LANGUAGE)

    def walk(self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str) -> FileFacts:
        facts = FileFacts(module_qname=module_qname, rel_path=rel_path, abs_path=abs_path)
        tree = self._parser.parse(source)
        # Positions that must NOT become reference candidates because they are already
        # accounted for (definition names, parameters, call targets, attribute names,
        # import names, base-class names).
        excluded: set[tuple[int, int]] = set()
        candidates: list[tuple[int, int, str, str]] = []  # (line, col, text, enclosing)
        stack = [_Frame(module_qname, "module")]
        self._visit(tree.root_node, stack, facts, excluded, candidates)

        for line, col, text, enclosing in candidates:
            if (line, col) not in excluded:
                facts.names.append(
                    RawName(NAME_REFERENCE, enclosing, line, col, text)
                )
        return facts

    # ------------------------------------------------------------------ #

    def _enclosing_symbol(self, stack: list[_Frame]) -> str:
        for frame in reversed(stack):
            if frame.kind == "function":
                return frame.qname
        return stack[0].qname  # module level

    def _visit(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[tuple[int, int, str, str]],
    ) -> None:
        node_type = node.type
        if node_type == "function_definition":
            self._visit_def(node, stack, facts, excluded, candidates, "function")
            return
        if node_type == "class_definition":
            self._visit_def(node, stack, facts, excluded, candidates, "class")
            return
        if node_type == "call":
            self._visit_call(node, stack, facts, excluded, candidates)
            return
        if node_type in ("import_statement", "import_from_statement"):
            self._visit_import(node, stack, facts, excluded)
            return
        if node_type == "attribute":
            attr = node.child_by_field_name("attribute")
            if attr is not None:
                excluded.add(_point(attr))
            obj = node.child_by_field_name("object")
            if obj is not None:
                self._visit(obj, stack, facts, excluded, candidates)
            return
        if node_type == "identifier":
            candidates.append((*_point(node), _text(node), self._enclosing_symbol(stack)))
            return
        for child in node.children:
            self._visit(child, stack, facts, excluded, candidates)

    def _visit_def(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[tuple[int, int, str, str]],
        kind: str,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}"

        if kind == "function":
            params = node.child_by_field_name("parameters")
            signature = name + (_text(params) if params is not None else "()")
            if params is not None:
                _exclude_all_identifiers(params, excluded)
        else:  # class
            supers = node.child_by_field_name("superclasses")
            signature = name + (_text(supers) if supers is not None else "()")
            if supers is not None:
                self._record_bases(supers, qname, facts, excluded)

        facts.symbols.append(
            RawSymbol(
                kind=NodeKind.SYMBOL,
                name=name,
                qualified_name=qname,
                container_qname=container,
                signature=signature,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                is_type_decl=(kind == "class"),
                body_hash=body_hash(node.child_by_field_name("body")),
            )
        )

        stack.append(_Frame(qname, kind))
        body = node.child_by_field_name("body")
        if body is not None:
            for child in body.children:
                self._visit(child, stack, facts, excluded, candidates)
        stack.pop()

    def _record_bases(
        self,
        supers: TSNode,
        class_qname: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        for child in supers.children:
            if child.type == "identifier":
                excluded.add(_point(child))
                facts.names.append(
                    RawName(NAME_INHERIT, class_qname, *_point(child), _text(child))
                )
            elif child.type == "attribute":
                attr = child.child_by_field_name("attribute")
                if attr is not None:
                    excluded.add(_point(attr))
                    # Resolve at the trailing name's position, but carry the full dotted
                    # path as text so an unresolved base keys its external node on the
                    # right top-level package (e.g. "ns.models.Base" -> "ns").
                    facts.names.append(
                        RawName(NAME_INHERIT, class_qname, *_point(attr), _text(child))
                    )

    def _visit_call(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[tuple[int, int, str, str]],
    ) -> None:
        fn = node.child_by_field_name("function")
        enclosing = self._enclosing_symbol(stack)
        if fn is not None:
            target = fn.child_by_field_name("attribute") if fn.type == "attribute" else fn
            if target is not None and target.type == "identifier":
                excluded.add(_point(target))
                facts.names.append(
                    RawName(NAME_CALL, enclosing, *_point(target), _text(target))
                )
            # Recurse the receiver of an attribute call (e.g. ``os`` in os.getpid()).
            if fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                if obj is not None:
                    self._visit(obj, stack, facts, excluded, candidates)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for child in args.children:
                self._visit(child, stack, facts, excluded, candidates)

    def _visit_import(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        module_qname = stack[0].qname
        if node.type == "import_statement":
            # `import a.b, c.d as e` — each child is an imported module; its own dotted
            # name is both the resolution target and the package label.
            for child in node.children:
                if child.type == "dotted_name":
                    self._emit_import(child, module_qname, facts, excluded, _text(child))
                elif child.type == "aliased_import":
                    inner = child.child_by_field_name("name")
                    if inner is not None:
                        self._emit_import(inner, module_qname, facts, excluded, _text(inner))
        else:
            # `from M import a, b as c` — M is the package; bind each imported NAME and
            # label its (possibly external) package with M, not the symbol name.
            module_node = node.child_by_field_name("module_name")
            module_text = _text(module_node) if module_node is not None else ""
            imported = node.children_by_field_name("name")
            if imported:
                for name_node in imported:
                    if name_node.type == "aliased_import":
                        inner = name_node.child_by_field_name("name")
                        if inner is not None:
                            self._emit_import(inner, module_qname, facts, excluded, module_text)
                    else:
                        self._emit_import(name_node, module_qname, facts, excluded, module_text)
            elif module_node is not None:
                # `from M import *` — the imported names are unknown, but the dependency on
                # the module M is not. Record that edge so M stays visible in the graph.
                self._emit_import(module_node, module_qname, facts, excluded, module_text)

    def _emit_import(
        self,
        name_node: TSNode,
        module_qname: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        module: str,
    ) -> None:
        # Resolve at the FIRST identifier of the name (jedi binds the leading component,
        # e.g. ``a`` in ``a.b.c``, or ``base`` in the relative ``.base``); carry the full
        # text for labelling. Skip if there is no identifier to bind (e.g. bare ``.``).
        target = _first_identifier(name_node)
        if target is None:
            return
        excluded.add(_point(target))
        facts.names.append(
            RawName(NAME_IMPORT, module_qname, *_point(target), _text(name_node), module)
        )


def extract_signatures(source: str) -> list[str]:
    """Return the raw signatures of every function/class defined in a code snippet.

    Used by the surfaces (pre-commit / PreToolUse) to pull the symbols an agent proposes
    to add out of a diff or tool payload, so each can be checked against the graph.
    """
    facts = TreeSitterWalker().walk(
        source.encode("utf-8"), module_qname="<snippet>", rel_path="<snippet>", abs_path="<snippet>"
    )
    return [sym.signature for sym in facts.symbols]


def _first_identifier(node: TSNode) -> TSNode | None:
    """The leading identifier to resolve for an import name (dotted or relative)."""
    if node.type == "identifier":
        return node
    if node.type == "dotted_name":
        return node.child(0)
    if node.type == "relative_import":
        # e.g. ``.base`` -> the ``base`` dotted_name's first identifier; bare ``.`` -> None.
        for child in node.children:
            if child.type == "dotted_name":
                return child.child(0)
        return None
    return node


def _exclude_all_identifiers(node: TSNode, excluded: set[tuple[int, int]]) -> None:
    if node.type == "identifier":
        excluded.add(_point(node))
    for child in node.children:
        _exclude_all_identifiers(child, excluded)
