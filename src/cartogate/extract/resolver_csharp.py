"""Pure-Python C# name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for C#, mirroring the
Java resolver: it parses each file once with tree-sitter, builds a fully-qualified-name index of
the repo's types (and their methods) keyed by **declared namespace** (``namespace X.Y { class T }``
→ ``X.Y.T``), then binds a name occurrence at ``(line, column)`` using **namespace + ``using`` +
enclosing-type** rules — no compiler, no MSBuild, no NuGet.

Unlike Java (where the package is the directory), C# declares its namespace *in source*, so the
namespace is read from the file. The resolver returns a definition's ``(path, line)``; the pipeline
maps that to the node, so this FQN scheme stays internal to resolution and never has to match a
node's qname.

Bound soundly (never a wrong edge):
- **types** in ``using Alias = T`` / base lists / ``new T()`` / parameter, field, return, and local
  types → the in-repo type, resolved through the enclosing namespace (and its ancestors) and the
  file's ``using`` namespaces (a single unambiguous match), else external/unresolved.
- **calls**: unqualified or ``this``/``base`` calls → a method of the enclosing type; ``Type.M()``
  where ``Type`` resolves → that type's static-style method; ``x.M()`` where ``x``'s type is
  *declared* in scope (a field/parameter/local/foreach var — read, not inferred) → that type's M.

Honest ceiling (returns ``None`` → no edge): a receiver whose type is inferred (``var x = Make()``),
an inherited/overloaded target, ambiguous ``using`` matches, a plain ``using Namespace`` (a
namespace, not a type, so it stays an external dependency), and anything outside the repo.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_CSHARP_LANGUAGE = Language(tscsharp.language())
_TYPE_DECLS = frozenset({
    "class_declaration", "interface_declaration", "struct_declaration",
    "enum_declaration", "record_declaration", "record_struct_declaration",
})
_CALLABLE_DECLS = frozenset({"method_declaration", "constructor_declaration"})
_NAMESPACE_DECLS = frozenset({"namespace_declaration", "file_scoped_namespace_declaration"})


class CSharpResolver:
    """Resolves C# name occurrences against an in-repo, namespace-keyed FQN index."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_CSHARP_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._type_def: dict[str, tuple[str, int]] = {}  # FQN -> (abs_path, def_line)
        self._methods: dict[str, dict[str, tuple[str, int]]] = {}  # typeFQN -> name -> (abs, line)
        #: (typeFQN, method) seen more than once — an overload set; a call to it is left unresolved.
        self._overloaded: set[tuple[str, str]] = set()
        self._usings: dict[str, list[str]] = {}  # abs -> [namespace, ...] (plain + static)
        self._aliases: dict[str, dict[str, str]] = {}  # abs -> alias -> target FQN
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        tree = self._parser.parse(text.encode("utf-8"))
        self._trees[abs_path] = tree.root_node
        self._usings[abs_path] = []
        self._aliases[abs_path] = {}
        self._read_usings(tree.root_node, abs_path)
        self._read_types(tree.root_node, "", abs_path)

    def _read_usings(self, root: TSNode, abs_path: str) -> None:
        for node in _descendants(root, {"using_directive"}):
            alias_node = node.child_by_field_name("name")  # set only for ``using Alias = Target;``
            target = _using_target(node, alias_node)
            if target is None:
                continue
            if alias_node is not None:  # ``using Alias = A.B.Type;`` — bind alias -> target FQN
                self._aliases[abs_path][_text(alias_node)] = _text(target)
            else:  # ``using A.B;`` / ``using static A.B;`` — the namespace comes into scope
                self._usings[abs_path].append(_text(target))

    def _read_types(self, node: TSNode, namespace: str, abs_path: str) -> None:
        """Walk declarations, threading the current namespace through namespace wrappers."""
        for child in node.children:
            if child.type in _NAMESPACE_DECLS:
                ns_name = child.child_by_field_name("name")
                inner = f"{namespace}.{_text(ns_name)}" if namespace and ns_name else (
                    _text(ns_name) if ns_name else namespace
                )
                body = child.child_by_field_name("body")
                # A file-scoped namespace has no body — its siblings (the rest of the file) belong
                # to it; recurse over this declaration's own remaining children either way.
                self._read_types(body if body is not None else child, inner, abs_path)
            elif child.type in _TYPE_DECLS:
                self._index_type(child, namespace, abs_path)
            else:
                self._read_types(child, namespace, abs_path)

    def _index_type(self, node: TSNode, container_fqn: str, abs_path: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        fqn = f"{container_fqn}.{_text(name_node)}" if container_fqn else _text(name_node)
        self._type_def[fqn] = (abs_path, node.start_point[0] + 1)
        self._methods.setdefault(fqn, {})
        body = node.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type in _CALLABLE_DECLS:
                m_name = member.child_by_field_name("name")
                if m_name is not None:
                    name = _text(m_name)
                    if name in self._methods[fqn]:
                        self._overloaded.add((fqn, name))  # second decl → overloaded
                    else:
                        self._methods[fqn][name] = (abs_path, member.start_point[0] + 1)
            elif member.type in _TYPE_DECLS:
                self._index_type(member, fqn, abs_path)  # nested type

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        using = _ancestor(node, {"using_directive"})
        if using is not None:
            return self._resolve_using(using, abs_path)

        invocation = _ancestor(node, {"invocation_expression"})
        if invocation is not None and _is_call_name(invocation, node):
            return self._resolve_call(invocation, abs_path)

        # Otherwise a type position (base/new/param/return/field/local type).
        return self._resolve_type(_text(node), abs_path, node)

    def _resolve_using(self, using: TSNode, abs_path: str) -> Resolved | None:
        # Only an *alias* using that targets an in-repo type resolves; a plain ``using Namespace``
        # is a namespace (no single definition) → unresolved (an external dependency edge).
        alias_node = using.child_by_field_name("name")
        target = _using_target(using, alias_node)
        if target is None:
            return None
        fqn = _text(target)
        hit = self._type_def.get(fqn)
        if hit is None:
            return None
        return Resolved(fqn.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")

    def _resolve_type(self, name: str, abs_path: str, node: TSNode) -> Resolved | None:
        fqn = self._type_fqn(name, abs_path, node)
        if fqn is None:
            return None
        hit = self._type_def.get(fqn)
        if hit is None:
            return None
        return Resolved(name.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")

    def _type_fqn(self, name: str, abs_path: str, node: TSNode | None) -> str | None:
        """Resolve a (possibly simple) type name to an in-repo FQN, soundly (None if unsure)."""
        if name in self._type_def:  # already fully-qualified and in-repo
            return name
        if "." in name:
            # A *qualified* name absent from the repo must not fall through to a same-tail import.
            return None
        # An alias (``using Foo = A.B.Bar;``) bound in this file.
        aliased = self._aliases.get(abs_path, {}).get(name)
        if aliased is not None:
            return aliased if aliased in self._type_def else None
        # The enclosing namespace and each ancestor (``A.B`` also sees ``A`` and the global scope).
        for ns in self._namespace_scopes(node):
            candidate = f"{ns}.{name}" if ns else name
            if candidate in self._type_def:
                return candidate
        # A ``using``'d namespace — a single unambiguous match (else unresolved).
        matches = [
            f"{ns}.{name}" for ns in self._usings.get(abs_path, [])
            if f"{ns}.{name}" in self._type_def
        ]
        return matches[0] if len(matches) == 1 else None

    def _resolve_call(self, invocation: TSNode, abs_path: str) -> Resolved | None:
        func = invocation.child_by_field_name("function")
        if func is None:
            return None

        if func.type == "identifier":  # unqualified ``M(...)`` → a method of the enclosing type
            method = _text(func)
            enclosing = self._enclosing_type_fqn(invocation, abs_path)
            if enclosing is not None and method in self._methods.get(enclosing, {}):
                return self._method_resolved(method, enclosing)
            return None

        if func.type != "member_access_expression":
            return None
        name_node = func.child_by_field_name("name")
        receiver = func.child_by_field_name("expression")
        if name_node is None or receiver is None:
            return None
        method = _text(name_node)

        if receiver.type in ("this_expression", "base_expression") or _text(receiver) in (
            "this", "base"
        ):
            enclosing = self._enclosing_type_fqn(invocation, abs_path)
            if enclosing is not None and method in self._methods.get(enclosing, {}):
                return self._method_resolved(method, enclosing)
            return None

        if receiver.type == "identifier":
            # ``Type.M()`` — receiver is itself a known type (static-style)...
            owner_fqn = self._type_fqn(_text(receiver), abs_path, receiver)
            # ...else ``var.M()`` where ``var`` has an explicitly DECLARED type in scope (F-69:
            # sound — declared, not inferred). External/primitive receivers stay unresolved.
            if owner_fqn is None:
                owner_fqn = self._receiver_type(receiver, abs_path)
            if owner_fqn is not None and method in self._methods.get(owner_fqn, {}):
                return self._method_resolved(method, owner_fqn)
        return None  # value receiver with no in-repo declared type — sound ceiling

    def _receiver_type(self, obj: TSNode, abs_path: str) -> str | None:
        """The in-repo FQN of receiver variable ``obj``'s DECLARED type, or ``None``. Reads the
        explicit ``Type name`` declaration nearest in scope — no inference."""
        type_name = _declared_type(obj, _text(obj))
        return self._type_fqn(type_name, abs_path, obj) if type_name is not None else None

    def _method_resolved(self, method: str, type_fqn: str) -> Resolved | None:
        # An overloaded method can't be pinned to one declaration without argument-type inference,
        # so leave it unresolved rather than link to an arbitrary overload (no wrong edge).
        if (type_fqn, method) in self._overloaded:
            return None
        abs_path, line = self._methods[type_fqn][method]
        return Resolved(method, f"{type_fqn}.{method}", Path(abs_path), line, "function")

    def _enclosing_type_fqn(self, node: TSNode, abs_path: str) -> str | None:
        decl = _ancestor(node, _TYPE_DECLS)
        if decl is None:
            return None
        target_line = decl.start_point[0] + 1
        for fqn, (path, line) in self._type_def.items():
            if path == abs_path and line == target_line:
                return fqn
        return None

    @staticmethod
    def _namespace_scopes(node: TSNode | None) -> list[str]:
        """The enclosing namespace and each ancestor prefix, longest-first, ending with the global
        ``""``. ``namespace A.B`` → ``["A.B", "A", ""]`` so a type in ``A`` is seen from ``A.B``."""
        ns = ""
        cur = node.parent if node is not None else None
        while cur is not None:
            if cur.type in _NAMESPACE_DECLS:
                name = cur.child_by_field_name("name")
                if name is not None:
                    ns = f"{_text(name)}.{ns}".rstrip(".") if ns else _text(name)
            cur = cur.parent
        scopes: list[str] = []
        parts = ns.split(".") if ns else []
        for i in range(len(parts), 0, -1):
            scopes.append(".".join(parts[:i]))
        scopes.append("")
        return scopes


def _declared_type(obj: TSNode, name: str) -> str | None:
    """Simple type name declared for variable ``name`` in the nearest enclosing scope, or ``None``.

    Walks ancestors call-site-outward so a shadowing inner declaration wins. A *local* declaration
    counts only if it lexically precedes the use. Parameters, foreach variables, and fields are
    always in scope. Returns the simple type name; ``var``/primitives/unknown shapes yield ``None``.
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
    """Type ``name`` declared within this one scope, or ``None``. Handles a method/constructor
    parameter, a ``foreach`` variable, a field, and a local variable (a local only if it precedes
    ``before``). ``var`` and primitive types yield ``None`` (sound: no inferred receiver)."""
    params = scope.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type == "parameter":
                nm = param.child_by_field_name("name")
                if nm is not None and _text(nm) == name:
                    return _simple_type_name(param.child_by_field_name("type"))
    if scope.type == "foreach_statement":  # foreach (Foo x in items)
        nm = scope.child_by_field_name("left")
        if nm is not None and _text(nm) == name:
            return _simple_type_name(scope.child_by_field_name("type"))
    for child in scope.children:
        if child.type == "field_declaration" or (
            child.type == "local_declaration_statement" and child.start_point < before
        ):
            var_decl = next((c for c in child.children if c.type == "variable_declaration"), None)
            if var_decl is None:
                continue
            type_node = var_decl.child_by_field_name("type")
            for decl in var_decl.children:
                if decl.type == "variable_declarator":
                    nm = decl.child_by_field_name("name")
                    if nm is not None and _text(nm) == name:
                        return _simple_type_name(type_node)
    return None


def _simple_type_name(type_node: TSNode | None) -> str | None:
    """The simple class name of a C# type node, or ``None`` for ``var``/primitives/unknowns."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "identifier":
        return _text(type_node)
    if t == "qualified_name":  # a.b.C -> keep qualified (resolved exactly, not stripped to a tail)
        return _text(type_node)
    if t == "generic_name":  # List<...> -> List
        base = type_node.child_by_field_name("name") or next(
            (c for c in type_node.children if c.type == "identifier"), None
        )
        return _text(base) if base is not None else None
    if t in ("nullable_type", "array_type"):
        inner = type_node.child_by_field_name("type") or next(
            (c for c in type_node.children if c.is_named), None
        )
        return _simple_type_name(inner)
    return None  # implicit_type (var) / predefined_type / unknown — not an in-repo class receiver


def _using_target(using: TSNode, alias_node: TSNode | None) -> TSNode | None:
    """The namespace/type node a ``using`` brings in: the trailing name, skipping the ``static``
    keyword and (for ``using Alias = Target``) the alias identifier itself."""
    for child in using.children:
        # Compare by the stable ``Node.id`` — tree-sitter wrapper objects lack Python identity.
        if child.type in ("qualified_name", "identifier") and (
            alias_node is None or child.id != alias_node.id
        ):
            return child
    return None


def _descendants(node: TSNode, types: frozenset[str] | set[str]) -> list[TSNode]:
    out: list[TSNode] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in types:
            out.append(cur)
        stack.extend(cur.children)
    return out


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_call_name(invocation: TSNode, node: TSNode) -> bool:
    """``node`` is the method-name token of ``invocation`` (unqualified or ``recv.Name``)."""
    func = invocation.child_by_field_name("function")
    if func is None:
        return False
    if func.type == "identifier":
        return func.start_point == node.start_point
    if func.type == "member_access_expression":
        name_node = func.child_by_field_name("name")
        return name_node is not None and name_node.start_point == node.start_point
    return False
