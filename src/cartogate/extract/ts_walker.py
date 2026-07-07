"""Tree-sitter AST walk for TypeScript → raw structural facts (spec §5.1, FUTURE F-08).

Deterministic and model-free, mirroring the Python walker (``ast_walker.py``) and emitting the
**same** ``RawSymbol`` / ``RawName`` / ``FileFacts``, so the rest of the pipeline, the resolver
contract, and the duplicate gate stay language-neutral.

It extracts symbol definitions (functions, arrow-function consts, classes + methods, interfaces)
*and* the positions of the names a resolver must bind: call targets, ``extends``/``implements``
bases, imported names, and other in-scope references. Binding those positions to nodes is the
resolver's job (``resolver_ts.py``); this module never guesses a target. Type-position names
(annotations, generic args) are intentionally not emitted in v1.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_typescript as tstypescript
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

#: The compiled TypeScript grammar, constructed once and shared by all walkers.
_TS_LANGUAGE = Language(tstypescript.language_typescript())

_CLASS_TYPES = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration"}
)
_ARROW_VALUES = frozenset({"arrow_function", "function", "function_expression"})
_METHOD_TYPES = frozenset({"method_definition", "method_signature", "abstract_method_signature"})
_PRIVATE_MODIFIERS = frozenset({"private", "protected"})
#: Type-position subtrees whose identifiers must NOT become value references (no type resolution).
_TYPE_NODES = frozenset({"type_annotation", "type_arguments", "type_parameters"})
#: JSX element tags (only in the ``tsx`` grammar the JS walker uses). A capitalized tag is a
#: React component reference; a lowercase tag (``div``) is an HTML element and is skipped.
_JSX_ELEMENTS = frozenset(
    {"jsx_opening_element", "jsx_self_closing_element", "jsx_closing_element"}
)

_Candidate = tuple[int, int, str, str]  # line, col, text, enclosing_qname


@dataclass(slots=True)
class _Frame:
    qname: str
    kind: str  # "module" | "class" | "function"


class TypeScriptWalker:
    """Walks TypeScript source into :class:`FileFacts`. Reusable across files.

    ``language`` defaults to the TypeScript grammar; the JavaScript walker reuses this class with
    the ``tsx`` grammar (a JS superset that also parses JSX) — see ``js_walker.py``.
    """

    def __init__(self, language: Language = _TS_LANGUAGE) -> None:
        self._parser = Parser(language)

    def walk(self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str) -> FileFacts:
        facts = FileFacts(module_qname=module_qname, rel_path=rel_path, abs_path=abs_path)
        tree = self._parser.parse(source)
        excluded: set[tuple[int, int]] = set()  # positions already accounted for (defs, calls, …)
        candidates: list[_Candidate] = []  # bare identifiers; the leftover become references
        self._visit(tree.root_node, [_Frame(module_qname, "module")], facts, excluded, candidates,
                    exported=False)
        for line, col, text, enclosing in candidates:
            if (line, col) not in excluded:
                facts.names.append(RawName(NAME_REFERENCE, enclosing, line, col, text))
        return facts

    # ------------------------------------------------------------------ #

    def _enclosing(self, stack: list[_Frame]) -> str:
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
        candidates: list[_Candidate],
        *,
        exported: bool,
    ) -> None:
        node_type = node.type
        if node_type == "export_statement":
            for child in node.named_children:
                self._visit(child, stack, facts, excluded, candidates, exported=True)
            return
        if node_type == "function_declaration":
            self._callable(node, node, stack, facts, excluded, candidates, _vis(exported))
            return
        if node_type == "lexical_declaration":
            self._lexical(node, stack, facts, excluded, candidates, exported)
            return
        if node_type in _CLASS_TYPES:
            self._class(node, stack, facts, excluded, candidates, _vis(exported))
            return
        if node_type in ("call_expression", "new_expression"):
            self._call(node, stack, facts, excluded, candidates)
            return
        if node_type == "import_statement":
            self._import(node, stack[0].qname, facts, excluded)
            return
        if node_type == "member_expression":
            prop = node.child_by_field_name("property")
            if prop is not None:
                excluded.add(_point(prop))  # a member access is not a free reference
            obj = node.child_by_field_name("object")
            if obj is not None:
                self._visit(obj, stack, facts, excluded, candidates, exported=False)
            return
        if node_type in _TYPE_NODES:
            return  # do not treat type-position identifiers as value references (v1)
        if node_type in _JSX_ELEMENTS:
            self._jsx(node, stack, facts, excluded, candidates)
            return
        if node_type == "identifier":
            candidates.append((*_point(node), _text(node), self._enclosing(stack)))
            return
        for child in node.named_children:
            self._visit(child, stack, facts, excluded, candidates, exported=False)

    def _callable(
        self,
        name_owner: TSNode,
        body_owner: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
        visibility: Visibility,
    ) -> None:
        name_node = name_owner.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}"
        params = body_owner.child_by_field_name("parameters")
        signature = name + (_text(params) if params is not None else "()")
        if params is not None:
            _exclude_identifiers(params, excluded)
        facts.symbols.append(
            _symbol(
                name, qname, container, signature, name_owner, visibility, body_owner=body_owner
            )
        )
        self._descend_body(body_owner, qname, stack, facts, excluded, candidates)

    def _lexical(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
        exported: bool,
    ) -> None:
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            value = decl.child_by_field_name("value")
            if value is not None and value.type in _ARROW_VALUES:
                self._callable(decl, value, stack, facts, excluded, candidates, _vis(exported))
            else:
                name_node = decl.child_by_field_name("name")
                if name_node is not None:
                    excluded.add(_point(name_node))  # the binding name is a def, not a reference
                if value is not None:  # but resolve calls/refs in the initializer
                    self._visit(value, stack, facts, excluded, candidates, exported=False)

    def _class(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
        visibility: Visibility,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}"
        bases: list[str] = []
        self._record_bases(node, qname, facts, excluded, bases)
        facts.symbols.append(
            _symbol(
                name, qname, container, f"{name}({','.join(bases)})", node, visibility,
                is_type_decl=True,
            )
        )
        stack.append(_Frame(qname, "class"))
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                if member.type in _METHOD_TYPES:
                    self._method(member, stack, facts, excluded, candidates)
        stack.pop()

    def _method(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        excluded.add(_point(name_node))
        name = _text(name_node)
        container = stack[-1].qname
        qname = f"{container}.{name}"
        params = node.child_by_field_name("parameters")
        signature = name + (_text(params) if params is not None else "()")
        if params is not None:
            _exclude_identifiers(params, excluded)
        visibility = Visibility.INTERNAL if _is_private(node) else Visibility.PUBLIC
        facts.symbols.append(_symbol(name, qname, container, signature, node, visibility))
        self._descend_body(node, qname, stack, facts, excluded, candidates)

    def _descend_body(
        self,
        owner: TSNode,
        qname: str,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
    ) -> None:
        body = owner.child_by_field_name("body")
        if body is None:
            return
        stack.append(_Frame(qname, "function"))
        self._visit(body, stack, facts, excluded, candidates, exported=False)
        stack.pop()

    def _jsx(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
    ) -> None:
        """A JSX tag: a capitalized component name (``<Foo/>``) is a reference; ``<div>`` is not.

        The matching ``</Foo>`` closing tag is the same element, so the reference is emitted only on
        the opener. Member-expression names (``<Foo.Bar/>``) are left unresolved — a sound ceiling.
        Attribute and child expressions are walked normally so calls/refs inside ``{...}`` resolve.
        """
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.type == "identifier":
            excluded.add(_point(name_node))  # a tag name is never a free reference
            text = _text(name_node)
            if text[:1].isupper() and node.type != "jsx_closing_element":
                facts.names.append(
                    RawName(NAME_REFERENCE, self._enclosing(stack), *_point(name_node), text)
                )
        for child in node.named_children:
            if child is name_node:
                continue
            self._visit(child, stack, facts, excluded, candidates, exported=False)

    def _call(
        self,
        node: TSNode,
        stack: list[_Frame],
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        candidates: list[_Candidate],
    ) -> None:
        enclosing = self._enclosing(stack)
        callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
        if callee is not None and callee.type == "member_expression":
            prop = callee.child_by_field_name("property")
            if prop is not None:
                excluded.add(_point(prop))
                facts.names.append(RawName(NAME_CALL, enclosing, *_point(prop), _text(prop)))
            obj = callee.child_by_field_name("object")
            if obj is not None:
                self._visit(obj, stack, facts, excluded, candidates, exported=False)
        elif callee is not None and callee.type == "identifier":
            excluded.add(_point(callee))
            facts.names.append(RawName(NAME_CALL, enclosing, *_point(callee), _text(callee)))
        elif callee is not None:
            self._visit(callee, stack, facts, excluded, candidates, exported=False)
        args = node.child_by_field_name("arguments")
        if args is not None:
            for child in args.named_children:
                self._visit(child, stack, facts, excluded, candidates, exported=False)

    def _import(
        self, node: TSNode, module_qname: str, facts: FileFacts, excluded: set[tuple[int, int]]
    ) -> None:
        source_node = node.child_by_field_name("source")
        source = _strip_quotes(_text(source_node)) if source_node is not None else ""
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":  # default import: `import X from "m"`
                self._emit_import(child, module_qname, source, facts, excluded)
            elif child.type == "namespace_import":  # `import * as ns from "m"`
                ident = next((c for c in child.named_children if c.type == "identifier"), None)
                if ident is not None:
                    self._emit_import(ident, module_qname, source, facts, excluded)
            elif child.type == "named_imports":  # `import { a, b as c } from "m"`
                for spec in child.named_children:
                    if spec.type != "import_specifier":
                        continue
                    local = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                    if local is not None:
                        self._emit_import(local, module_qname, source, facts, excluded)

    def _emit_import(
        self,
        local_node: TSNode,
        module_qname: str,
        source: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
    ) -> None:
        excluded.add(_point(local_node))
        facts.names.append(
            RawName(NAME_IMPORT, module_qname, *_point(local_node), _text(local_node), source)
        )

    def _record_bases(
        self,
        node: TSNode,
        class_qname: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        out: list[str],
    ) -> None:
        for child in node.children:
            if "heritage" in child.type or "extends" in child.type or "implements" in child.type:
                self._heritage_names(child, class_qname, facts, excluded, out)

    def _heritage_names(
        self,
        node: TSNode,
        class_qname: str,
        facts: FileFacts,
        excluded: set[tuple[int, int]],
        out: list[str],
    ) -> None:
        if node.type == "type_arguments":
            return  # skip generic args (the T in Base<T>)
        if node.type in ("type_identifier", "identifier"):
            excluded.add(_point(node))
            out.append(_text(node))
            facts.names.append(RawName(NAME_INHERIT, class_qname, *_point(node), _text(node)))
            return
        for child in node.children:
            self._heritage_names(child, class_qname, facts, excluded, out)


def _symbol(
    name: str,
    qname: str,
    container: str,
    signature: str,
    node: TSNode,
    visibility: Visibility,
    body_owner: TSNode | None = None,
    is_type_decl: bool = False,
) -> RawSymbol:
    # ``node`` carries the name/position; for an arrow const the body lives on a separate node
    # (the arrow/function value), so the caller passes it as ``body_owner``.
    return RawSymbol(
        kind=NodeKind.SYMBOL,
        name=name,
        qualified_name=qname,
        container_qname=container,
        signature=signature,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        visibility=visibility,
        is_type_decl=is_type_decl,
        body_hash=body_hash((body_owner or node).child_by_field_name("body")),
    )


def _vis(exported: bool) -> Visibility:
    return Visibility.EXPORTED if exported else Visibility.INTERNAL


def _is_private(node: TSNode) -> bool:
    return any(
        child.type == "accessibility_modifier" and _text(child) in _PRIVATE_MODIFIERS
        for child in node.children
    )


def _exclude_identifiers(node: TSNode, excluded: set[tuple[int, int]]) -> None:
    if node.type == "identifier":
        excluded.add(_point(node))
    for child in node.children:
        _exclude_identifiers(child, excluded)


def _strip_quotes(text: str) -> str:
    return text.strip().strip("\"'`")
