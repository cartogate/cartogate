"""Pure-Python Java name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for Java, mirroring
the TypeScript resolver: it parses each file once with tree-sitter, builds a fully-qualified-name
index of the repo's types (and their methods), then binds a name occurrence at ``(line, column)``
to a definition using **package + imports + same-package** rules — no compiler, no classpath.

Bound soundly (never a wrong edge):
- **types** in ``import`` / ``extends`` / ``implements`` / ``new T()`` / annotations and other
  type positions → the in-repo type, else external (left unresolved → an external node).
- **calls**: unqualified or ``this``/``super`` calls → a method of the enclosing type; a
  ``Type.m()`` static-style call where ``Type`` resolves → that type's method.

An instance call ``x.m()`` resolves when ``x``'s type is *declared* in scope (a ``Foo x`` param/
local/for-var/field — read, not inferred). Honest ceiling (returns ``None`` → no edge): a receiver
whose type is not declared in scope (inferred from a call/expression), ambiguous wildcard imports,
overloaded targets, and anything outside the repo.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_JAVA_LANGUAGE = Language(tsjava.language())
_TYPE_DECLS = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
})
_CALLABLE_DECLS = frozenset({"method_declaration", "constructor_declaration"})


class JavaResolver:
    """Resolves Java name occurrences against an in-repo FQN index."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_JAVA_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._type_def: dict[str, tuple[str, int]] = {}  # FQN -> (abs_path, def_line)
        self._methods: dict[str, dict[str, tuple[str, int]]] = {}  # typeFQN -> name -> (abs, line)
        #: (typeFQN, method) seen more than once — an overload set. A call to it can't be resolved
        #: to a single method without argument-type inference, so it is left unresolved (sound).
        self._overloaded: set[tuple[str, str]] = set()
        self._package: dict[str, str] = {}  # abs -> package
        self._imports: dict[str, dict[str, str]] = {}  # abs -> simple -> FQN
        self._static_imports: dict[str, dict[str, str]] = {}  # abs -> member -> typeFQN
        self._wildcards: dict[str, list[str]] = {}  # abs -> [package, ...]
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        tree = self._parser.parse(text.encode("utf-8"))
        self._trees[abs_path] = tree.root_node
        package = self._package_of(abs_path)
        self._package[abs_path] = package
        self._imports[abs_path] = {}
        self._static_imports[abs_path] = {}
        self._wildcards[abs_path] = []
        self._read_imports(tree.root_node, abs_path)
        self._read_types(tree.root_node, package, abs_path)

    def _package_of(self, abs_path: str) -> str:
        """The package = the file's directory relative to the project root, dotted."""
        try:
            rel = Path(abs_path).resolve().relative_to(self._root)
        except ValueError:
            return ""
        return ".".join(rel.parts[:-1])

    def _read_imports(self, root: TSNode, abs_path: str) -> None:
        for node in root.children:
            if node.type != "import_declaration":
                continue
            scoped = next(
                (c for c in node.children if c.type in ("scoped_identifier", "identifier")), None
            )
            if scoped is None:
                continue
            fqn = _text(scoped)
            is_static = any(c.type == "static" for c in node.children)
            is_wildcard = any(c.type == "asterisk" for c in node.children)
            if is_wildcard:
                self._wildcards[abs_path].append(fqn)  # the package (static-or-not)
            elif is_static:
                owner, _, member = fqn.rpartition(".")
                if member:
                    self._static_imports[abs_path][member] = owner
            else:
                self._imports[abs_path][fqn.rsplit(".", 1)[-1]] = fqn

    def _read_types(self, node: TSNode, container_fqn: str, abs_path: str) -> None:
        for child in self._type_children(node):
            if child.type in _TYPE_DECLS:
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                fqn = f"{container_fqn}.{_text(name_node)}" if container_fqn else _text(name_node)
                self._type_def[fqn] = (abs_path, child.start_point[0] + 1)
                self._methods.setdefault(fqn, {})
                body = child.child_by_field_name("body")
                if body is not None:
                    for member in body.children:
                        if member.type in _CALLABLE_DECLS:
                            m_name = member.child_by_field_name("name")
                            if m_name is not None:
                                name = _text(m_name)
                                if name in self._methods[fqn]:
                                    self._overloaded.add((fqn, name))  # second decl → overloaded
                                else:
                                    self._methods[fqn][name] = (abs_path, member.start_point[0] + 1)
                    self._read_types(body, fqn, abs_path)  # nested types

    @staticmethod
    def _type_children(node: TSNode) -> list[TSNode]:
        return list(node.children)

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        import_decl = _ancestor(node, {"import_declaration"})
        if import_decl is not None:
            return self._resolve_import(import_decl)

        invocation = _ancestor(node, {"method_invocation"})
        if invocation is not None and _is_method_name(invocation, node):
            return self._resolve_call(invocation, abs_path)

        # Otherwise a type position (extends/implements/new/param/return/field/local/annotation).
        return self._resolve_type(_text(node), abs_path)

    def _resolve_import(self, import_decl: TSNode) -> Resolved | None:
        scoped = next(
            (c for c in import_decl.children if c.type in ("scoped_identifier", "identifier")), None
        )
        if scoped is None:
            return None
        fqn = _text(scoped)
        hit = self._type_def.get(fqn)
        if hit is None:  # a wildcard package, a member, or an external import → external node
            return None
        return Resolved(fqn.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")

    def _resolve_type(self, name: str, abs_path: str) -> Resolved | None:
        fqn = self._type_fqn(name, abs_path)
        if fqn is None:
            return None
        hit = self._type_def.get(fqn)
        if hit is None:
            return None
        return Resolved(name.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")

    def _type_fqn(self, name: str, abs_path: str) -> str | None:
        """Resolve a (possibly simple) type name to an in-repo FQN, soundly (None if unsure)."""
        if name in self._type_def:  # already fully-qualified and in-repo
            return name
        if "." in name:
            # A *qualified* name absent from the repo must NOT fall through to a same-simple-name
            # import — ``a.b.Foo`` is a different class from an imported ``x.Foo`` (soundness).
            return None
        simple = name
        imported = self._imports.get(abs_path, {}).get(simple)
        if imported is not None:
            return imported if imported in self._type_def else None
        same_pkg = f"{self._package[abs_path]}.{simple}" if self._package.get(abs_path) else simple
        if same_pkg in self._type_def:
            return same_pkg
        matches = [
            f"{pkg}.{simple}" for pkg in self._wildcards.get(abs_path, [])
            if f"{pkg}.{simple}" in self._type_def
        ]
        return matches[0] if len(matches) == 1 else None  # ambiguous wildcard → unresolved

    def _resolve_call(self, invocation: TSNode, abs_path: str) -> Resolved | None:
        name_node = invocation.child_by_field_name("name")
        if name_node is None:
            return None
        method = _text(name_node)
        obj = invocation.child_by_field_name("object")

        if obj is None:
            enclosing = self._enclosing_type_fqn(invocation, abs_path)
            if enclosing is not None and method in self._methods.get(enclosing, {}):
                return self._method_resolved(method, enclosing)
            owner = self._static_imports.get(abs_path, {}).get(method)
            if owner is not None:
                owner_fqn = self._type_fqn(owner, abs_path)
                if owner_fqn and method in self._methods.get(owner_fqn, {}):
                    return self._method_resolved(method, owner_fqn)
            return None

        if obj.type in ("this", "super_expression") or _text(obj) in ("this", "super"):
            enclosing = self._enclosing_type_fqn(invocation, abs_path)
            if enclosing is not None and method in self._methods.get(enclosing, {}):
                return self._method_resolved(method, enclosing)
            return None

        if obj.type == "identifier":
            # `Type.method()` — the receiver is itself a known type (static-style)...
            owner_fqn = self._type_fqn(_text(obj), abs_path)
            # ...else `var.method()` where `var` has an explicitly DECLARED type in scope (F-69:
            # sound — the type is declared, not inferred). External/primitive types stay unresolved.
            if owner_fqn is None:
                owner_fqn = self._receiver_type(obj, abs_path)
            if owner_fqn is not None and method in self._methods.get(owner_fqn, {}):
                return self._method_resolved(method, owner_fqn)
        return None  # value receiver with no in-repo declared type — sound ceiling

    def _receiver_type(self, obj: TSNode, abs_path: str) -> str | None:
        """The in-repo FQN of receiver variable ``obj``'s DECLARED type (param/local/for-var), or
        ``None``. Reads the explicit ``Type name`` declaration nearest in scope — no inference."""
        type_name = _declared_type(obj, _text(obj))
        return self._type_fqn(type_name, abs_path) if type_name is not None else None

    def _method_resolved(self, method: str, type_fqn: str) -> Resolved | None:
        # An overloaded method can't be pinned to one declaration without argument-type inference,
        # so leave it unresolved rather than link the call to an arbitrary overload (no wrong edge).
        if (type_fqn, method) in self._overloaded:
            return None
        abs_path, line = self._methods[type_fqn][method]
        return Resolved(method, f"{type_fqn}.{method}", Path(abs_path), line, "function")

    def _enclosing_type_fqn(self, node: TSNode, abs_path: str) -> str | None:
        decl = _ancestor(node, _TYPE_DECLS)
        if decl is None:
            return None
        name_node = decl.child_by_field_name("name")
        if name_node is None:
            return None
        # Match the declaration's start line to the indexed FQN (handles nested types).
        target_line = decl.start_point[0] + 1
        for fqn, (path, line) in self._type_def.items():
            if path == abs_path and line == target_line:
                return fqn
        return None


def _declared_type(obj: TSNode, name: str) -> str | None:
    """Simple type name declared for variable ``name`` in the nearest enclosing scope, or ``None``.

    Walks ancestors call-site-outward so a shadowing inner declaration wins. A *local* declaration
    counts only if it lexically precedes the use (a later same-named local can't be this binding —
    soundness against a local shadowing a field). Params, for-variables, and fields are always in
    scope. Returns the simple type name (a class); primitives/arrays/unknown shapes yield ``None``.
    """
    before = obj.start_point
    scope: TSNode | None = obj.parent
    while scope is not None:
        found = _scope_declares(scope, name, before)
        if found is not None:
            return found
        scope = scope.parent
    return None


def _scope_declares(scope: TSNode, name: str, before: tuple[int, int]) -> str | None:
    """Type ``name`` is declared with in this one scope, or ``None``. Handles four patterns: a
    method/constructor/lambda parameter, an enhanced-for variable, a field, and a local variable
    (a local only if it precedes ``before`` — earlier than the use). Catch-clause params and
    try-with-resources are intentionally not handled (a recall ceiling, never a wrong edge)."""
    params = scope.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type in ("formal_parameter", "spread_parameter"):
                nm = param.child_by_field_name("name")
                if nm is not None and _text(nm) == name:
                    return _simple_type_name(param.child_by_field_name("type"))
    if scope.type == "enhanced_for_statement":  # for (Foo x : items)
        nm = scope.child_by_field_name("name")
        if nm is not None and _text(nm) == name:
            return _simple_type_name(scope.child_by_field_name("type"))
    for child in scope.children:
        if child.type == "field_declaration" or (
            child.type == "local_variable_declaration" and child.start_point < before
        ):
            type_node = child.child_by_field_name("type")
            for decl in child.children:
                if decl.type == "variable_declarator":
                    nm = decl.child_by_field_name("name")
                    if nm is not None and _text(nm) == name:
                        return _simple_type_name(type_node)
    return None


def _simple_type_name(type_node: TSNode | None) -> str | None:
    """The simple class name of a Java type node, or ``None`` for primitives/arrays/unknowns."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        return _text(type_node)
    if type_node.type == "generic_type":  # List<...> -> List
        base = next((c for c in type_node.children if c.type == "type_identifier"), None)
        return _text(base) if base is not None else None
    if type_node.type == "scoped_type_identifier":  # a.b.C -> keep qualified (resolved exactly,
        return _text(type_node)  # NOT stripped to the tail — that could match a different class
    return None  # void/primitive/array_type/etc. — not an in-repo class receiver


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_method_name(invocation: TSNode, node: TSNode) -> bool:
    name_node = invocation.child_by_field_name("name")
    return name_node is not None and name_node.start_point == node.start_point
