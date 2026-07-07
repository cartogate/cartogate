"""Pure-Python Kotlin name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for Kotlin, mirroring the
C# resolver: it parses each file once, reads the in-file ``package`` header + ``import`` directives,
and builds a package-keyed index of the repo's types (and their methods) and top-level functions,
then binds a name occurrence using **package + imports + enclosing-type** rules — no compiler, no
classpath.

Bound soundly (never a wrong edge):
- **types** in ``import`` / supertypes / parameter, property, return positions → the in-repo type.
- **constructor calls** ``User(...)`` → the class ``User`` (Kotlin has no ``new``).
- **calls**: an unqualified ``f()`` → a same-class method, else a top-level function; ``this.m()``
  → the enclosing type's method; ``obj.m()`` → the method of ``obj``'s *declared* type (a ``x: T``
  parameter/property — read, not inferred); ``Type.m()`` / ``Object.m()`` → that type's method.

Honest ceiling (returns ``None`` → no edge): an inferred receiver (``val x = make()``), extension
functions, overloaded targets, and anything outside the repo.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_kotlin as tskotlin
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_KOTLIN_LANGUAGE = Language(tskotlin.language())
_TYPE_DECLS = frozenset({"class_declaration", "object_declaration"})


class KotlinResolver:
    """Resolves Kotlin name occurrences against an in-repo, package-keyed index."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_KOTLIN_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._type_def: dict[str, tuple[str, int]] = {}  # FQN -> (abs, line)
        self._methods: dict[str, dict[str, tuple[str, int]]] = {}  # typeFQN -> name -> (abs, line)
        self._overloaded: set[tuple[str, str]] = set()
        self._functions: dict[str, tuple[str, int]] = {}  # FQN -> (abs, line); top-level functions
        self._overloaded_fn: set[str] = set()
        self._package: dict[str, str] = {}  # abs -> package
        self._imports: dict[str, dict[str, str]] = {}  # abs -> simple -> FQN
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        package = _package_of(root)
        self._package[abs_path] = package
        self._imports[abs_path] = {}
        self._read_imports(root, abs_path)
        self._read_decls(root, package, abs_path)

    def _read_imports(self, root: TSNode, abs_path: str) -> None:
        for node in root.children:
            if node.type != "import":
                continue
            qid = next((c for c in node.children if c.type == "qualified_identifier"), None)
            if qid is not None:
                fqn = _text(qid)
                self._imports[abs_path][fqn.rsplit(".", 1)[-1]] = fqn

    def _read_decls(self, node: TSNode, container_fqn: str, abs_path: str) -> None:
        for child in node.children:
            if child.type in _TYPE_DECLS:
                self._index_type(child, container_fqn, abs_path)
            elif child.type == "function_declaration":  # a top-level function
                name = child.child_by_field_name("name")
                if name is not None:
                    self._add_function(container_fqn, _text(name), abs_path,
                                       child.start_point[0] + 1)

    def _index_type(self, node: TSNode, container_fqn: str, abs_path: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        fqn = f"{container_fqn}.{_text(name_node)}" if container_fqn else _text(name_node)
        self._type_def[fqn] = (abs_path, node.start_point[0] + 1)
        self._methods.setdefault(fqn, {})
        body = next((c for c in node.children if c.type in ("class_body", "enum_class_body")), None)
        if body is None:
            return
        for member in body.children:
            if member.type == "function_declaration":
                m_name = member.child_by_field_name("name")
                if m_name is not None:
                    name = _text(m_name)
                    if name in self._methods[fqn]:
                        self._overloaded.add((fqn, name))
                    else:
                        self._methods[fqn][name] = (abs_path, member.start_point[0] + 1)
            elif member.type in _TYPE_DECLS:
                self._index_type(member, fqn, abs_path)  # nested type

    def _add_function(self, container_fqn: str, name: str, abs_path: str, line: int) -> None:
        fqn = f"{container_fqn}.{name}" if container_fqn else name
        if fqn in self._functions:
            self._overloaded_fn.add(fqn)
        else:
            self._functions[fqn] = (abs_path, line)

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        import_node = _ancestor(node, {"import"})
        if import_node is not None:
            qid = next((c for c in import_node.children if c.type == "qualified_identifier"), None)
            return self._resolve_named(_text(qid), abs_path) if qid is not None else None

        call = _ancestor(node, {"call_expression"})
        if call is not None and _is_call_name(call, node):
            return self._resolve_call(node, abs_path)

        if node.type in ("identifier", "type_identifier") and _ancestor(node, {"user_type"}):
            return self._resolve_type(_text(node), abs_path)
        return None

    def _resolve_call(self, node: TSNode, abs_path: str) -> Resolved | None:
        name = _text(node)
        parent = node.parent
        if parent is not None and parent.type == "navigation_expression":
            return self._resolve_member_call(parent, node, name, abs_path)
        # Unqualified ``name(...)`` — a constructor, a same-class method, or a top-level function.
        type_fqn = self._type_fqn(name, abs_path)
        if type_fqn is not None:
            hit = self._type_def[type_fqn]
            return Resolved(name, type_fqn, Path(hit[0]), hit[1], "class")  # constructor call
        enclosing = self._enclosing_type_fqn(node, abs_path)
        if enclosing is not None and name in self._methods.get(enclosing, {}):
            return self._method(name, enclosing)
        return self._free_function(name, abs_path)

    def _resolve_member_call(
        self, nav: TSNode, node: TSNode, method: str, abs_path: str
    ) -> Resolved | None:
        receiver = next((c for c in nav.children if c.is_named), None)
        if receiver is None or receiver.start_point == node.start_point:
            return None
        if receiver.type == "this_expression":
            enclosing = self._enclosing_type_fqn(node, abs_path)
            if enclosing is not None and method in self._methods.get(enclosing, {}):
                return self._method(method, enclosing)
            return None
        if receiver.type == "identifier":
            # ``Type.m()`` / ``Object.m()`` — receiver is itself a known type...
            owner = self._type_fqn(_text(receiver), abs_path)
            # ...else ``var.m()`` where ``var`` has an explicitly DECLARED type in scope.
            if owner is None:
                declared = _declared_type(receiver, _text(receiver))
                owner = self._type_fqn(declared, abs_path) if declared is not None else None
            if owner is not None and method in self._methods.get(owner, {}):
                return self._method(method, owner)
        return None

    def _resolve_type(self, name: str, abs_path: str) -> Resolved | None:
        fqn = self._type_fqn(name, abs_path)
        if fqn is None:
            return None
        hit = self._type_def[fqn]
        return Resolved(name.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")

    def _resolve_named(self, name: str, abs_path: str) -> Resolved | None:
        """Resolve an ``import`` tail to a type or a top-level function (else external)."""
        if name in self._type_def:
            hit = self._type_def[name]
            return Resolved(name.rsplit(".", 1)[-1], name, Path(hit[0]), hit[1], "class")
        if name in self._functions and name not in self._overloaded_fn:
            hit = self._functions[name]
            return Resolved(name.rsplit(".", 1)[-1], name, Path(hit[0]), hit[1], "function")
        return None

    def _type_fqn(self, name: str, abs_path: str) -> str | None:
        simple = name.rsplit(".", 1)[-1]
        if name in self._type_def:
            return name
        if "." in name:
            return None
        imported = self._imports.get(abs_path, {}).get(simple)
        if imported is not None:
            return imported if imported in self._type_def else None
        pkg = self._package.get(abs_path, "")
        candidate = f"{pkg}.{simple}" if pkg else simple
        return candidate if candidate in self._type_def else None

    def _free_function(self, name: str, abs_path: str) -> Resolved | None:
        imported = self._imports.get(abs_path, {}).get(name)
        pkg = self._package.get(abs_path, "")
        for fqn in (imported, f"{pkg}.{name}" if pkg else name, name):
            if fqn and fqn in self._functions and fqn not in self._overloaded_fn:
                hit = self._functions[fqn]
                return Resolved(name, fqn, Path(hit[0]), hit[1], "function")
        return None

    def _method(self, method: str, type_fqn: str) -> Resolved | None:
        if (type_fqn, method) in self._overloaded:
            return None
        abs_path, line = self._methods[type_fqn][method]
        return Resolved(method, f"{type_fqn}.{method}", Path(abs_path), line, "function")

    def _enclosing_type_fqn(self, node: TSNode, abs_path: str) -> str | None:
        decl = _ancestor(node, _TYPE_DECLS)
        if decl is None:
            return None
        target = decl.start_point[0] + 1
        for fqn, (path, line) in self._type_def.items():
            if path == abs_path and line == target:
                return fqn
        return None


# --- helpers ---------------------------------------------------------------- #


def _package_of(root: TSNode) -> str:
    header = next((c for c in root.children if c.type == "package_header"), None)
    if header is None:
        return ""
    qid = next((c for c in header.children if c.type == "qualified_identifier"), None)
    return _text(qid) if qid is not None else ""


def _declared_type(obj: TSNode, name: str) -> str | None:
    """The declared type name of variable ``name`` in the nearest enclosing scope, or ``None``.

    Reads an explicit ``name: Type`` parameter, property, or local — never an inferred ``val x =``.
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
    for child in _descendants(scope, {"parameter", "variable_declaration"}):
        if child.type == "variable_declaration" and child.start_point >= before:
            continue  # a local must lexically precede the use
        ident = next((c for c in child.children if c.type == "identifier"), None)
        if ident is not None and _text(ident) == name:
            return _user_type_name(next((c for c in child.children if c.type == "user_type"), None))
    return None


def _user_type_name(user_type: TSNode | None) -> str | None:
    if user_type is None:
        return None
    ids = [c for c in user_type.children if c.type in ("identifier", "type_identifier")]
    return _text(ids[-1]) if ids else None


def _descendants(node: TSNode, types: frozenset[str] | set[str]) -> list[TSNode]:
    out: list[TSNode] = []
    stack = list(node.children)
    while stack:
        cur = stack.pop()
        if cur.type in types:
            out.append(cur)
        else:
            stack.extend(cur.children)
    return out


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_call_name(call: TSNode, node: TSNode) -> bool:
    """``node`` is the callee name of ``call`` (an unqualified id or a navigation's trailing id)."""
    callee = next((c for c in call.children if c.is_named), None)
    if callee is None:
        return False
    if callee.type == "identifier":
        return callee.start_point == node.start_point
    if callee.type == "navigation_expression":
        ids = [c for c in callee.children if c.type == "identifier"]
        return bool(ids) and ids[-1].start_point == node.start_point
    return False
