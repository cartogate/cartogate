"""Pure-Python Go name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for Go. Go's explicit
imports and package-per-directory model make sound resolution tractable without a compiler:

- **imports** (``import "app/models"``) → the in-repo package's module node (else external).
- **selector calls/refs** (``models.NewUser()``, ``models.User``) → the func/type in the imported
  package (the local package name, honoring aliases).
- **same-package** calls/refs (``foo()``, ``User``) → the func/type declared in the same package.
- **embedding** (``type User struct { Base }``) → the embedded type.
- **typed-receiver method calls** (``x.Method()``) → the method, when ``x`` has an explicitly
  *declared* type in scope (a param/receiver or a ``var x T``).

Honest ceiling (returns ``None`` → no edge): a value receiver whose type is *inferred* (``x := …``,
a return value), an embedded/promoted or interface method, and any import whose path doesn't
resolve to an in-repo package.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import is_locally_shadowed
from cartogate.extract.treesitter_util import text as _text

_GO_LANGUAGE = Language(tsgo.language())


class GoResolver:
    """Resolves Go name occurrences against an in-repo package + symbol index."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_GO_LANGUAGE)
        self._module_prefix = _read_go_module(self._root)  # e.g. "github.com/org/repo" or ""
        self._trees: dict[str, TSNode] = {}
        self._file_pkg: dict[str, str] = {}  # abs -> package qname (dir-dotted)
        self._packages: set[str] = set()  # all in-repo package qnames
        self._pkg_file: dict[str, str] = {}  # package qname -> one abs file (for module targets)
        #: (packageQname, simpleName) -> (abs_path, def_line) for funcs/types (top-level).
        self._symbols: dict[tuple[str, str], tuple[str, int]] = {}
        #: (packageQname, typeName) -> {method -> (abs_path, def_line)} for receiver methods (F-69).
        self._methods: dict[tuple[str, str], dict[str, tuple[str, int]]] = {}
        #: abs -> localName -> packageQname (imports resolved to in-repo packages).
        self._imports: dict[str, dict[str, str]] = {}
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)
        # Imports need the full package set, so resolve them in a second pass.
        for abs_path, text in sources.items():
            self._index_imports(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        pkg = self._package_of(abs_path)
        self._file_pkg[abs_path] = pkg
        self._packages.add(pkg)
        self._pkg_file.setdefault(pkg, abs_path)
        for node in root.children:
            if node.type == "function_declaration":
                name = node.child_by_field_name("name")
                if name is not None:
                    self._symbols.setdefault(
                        (pkg, _text(name)), (abs_path, node.start_point[0] + 1)
                    )
            elif node.type == "type_declaration":
                for spec in node.children:
                    if spec.type == "type_spec":
                        name = spec.child_by_field_name("name")
                        if name is not None:
                            self._symbols.setdefault(
                                (pkg, _text(name)), (abs_path, spec.start_point[0] + 1)
                            )
            elif node.type == "method_declaration":  # func (r T) M() — index under receiver type
                name = node.child_by_field_name("name")
                recv_type = _receiver_type_name(node.child_by_field_name("receiver"))
                if name is not None and recv_type is not None:
                    self._methods.setdefault((pkg, recv_type), {}).setdefault(
                        _text(name), (abs_path, node.start_point[0] + 1)
                    )

    def _index_imports(self, abs_path: str, text: str) -> None:
        self._imports[abs_path] = {}
        root = self._trees[abs_path]
        for spec in _descend(root, "import_spec"):
            path_node = spec.child_by_field_name("path")
            if path_node is None:
                continue
            path = _text(path_node).strip().strip('"`')
            pkg = self._import_to_package(path)
            if pkg is None:
                continue  # external
            alias = spec.child_by_field_name("name")
            local = _text(alias) if alias is not None else path.rsplit("/", 1)[-1]
            self._imports[abs_path][local] = pkg

    def _package_of(self, abs_path: str) -> str:
        try:
            rel = Path(abs_path).resolve().relative_to(self._root)
        except ValueError:
            return ""
        return ".".join(rel.parts[:-1])

    def _import_to_package(self, path: str) -> str | None:
        """Map an import path to an in-repo package qname, else ``None`` (external)."""
        dotted = path.replace("/", ".")
        if self._module_prefix and dotted.startswith(self._module_prefix + "."):
            dotted = dotted[len(self._module_prefix) + 1:]
        if dotted in self._packages:
            return dotted
        # Fall back to a unique suffix match against in-repo packages.
        tail = path.rsplit("/", 1)[-1]
        matches = [p for p in self._packages if p == tail or p.endswith("." + tail)]
        return matches[0] if len(matches) == 1 else None

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        if _ancestor(node, {"import_spec"}) is not None:
            return self._resolve_import(node, abs_path)

        name = _text(node)
        # A selector (`pkg.Name` / `x.Method`): resolve against the imported package symbol, or —
        # if the operand is a variable with a declared type — against the receiver-method index.
        selector = _ancestor(node, {"selector_expression", "qualified_type"})
        if selector is not None:
            operand = selector.child_by_field_name("operand") or selector.child_by_field_name(
                "package"
            )
            local = _text(operand) if operand is not None else ""
            # A local variable can shadow an import alias — then the operand is the local, not the
            # package. If it has a DECLARED type, resolve the method on it (F-69, sound); else None.
            if operand is not None and _is_locally_shadowed(operand, local):
                return self._receiver_method(operand, name, abs_path)
            pkg = self._imports.get(abs_path, {}).get(local)
            if pkg is not None:
                return self._symbol_resolved(pkg, name)  # pkg.Name
            # not an import alias — maybe a local var of a declared type (e.g. a package-less file)
            if operand is not None and operand.type == "identifier":
                return self._receiver_method(operand, name, abs_path)
            return None  # value receiver with no declared type → unresolved (sound ceiling)

        # An unqualified name → a func/type in the same package, unless a param or local
        # declaration in an enclosing function scope shadows it (then it is the local).
        if _is_locally_shadowed(node, name):
            return None
        return self._symbol_resolved(self._file_pkg.get(abs_path, ""), name)

    def _resolve_import(self, node: TSNode, abs_path: str) -> Resolved | None:
        spec = _ancestor(node, {"import_spec"})
        if spec is None:
            return None
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            return None
        pkg = self._import_to_package(_text(path_node).strip().strip('"`'))
        if pkg is None:
            return None  # external package
        file_abs = self._pkg_file.get(pkg)
        if file_abs is None:
            return None
        # Target the package's module node (any file of the package; def_type "module").
        return Resolved(pkg.rsplit(".", 1)[-1], pkg, Path(file_abs), None, "module")

    def _symbol_resolved(self, pkg: str, name: str) -> Resolved | None:
        hit = self._symbols.get((pkg, name))
        if hit is None:
            return None
        return Resolved(name, f"{pkg}.{name}", Path(hit[0]), hit[1], "class")

    def _receiver_method(self, operand: TSNode, method: str, abs_path: str) -> Resolved | None:
        """Resolve ``x.method()`` when ``x`` has an explicitly DECLARED type (a param or a
        ``var x T``) in scope — sound (the type is declared, not inferred). ``None`` otherwise."""
        raw = _declared_type(operand, _text(operand))
        if raw is None:
            return None
        pkg, type_name = self._type_pkg(raw, abs_path)
        if type_name is None:
            return None
        hit = self._methods.get((pkg, type_name), {}).get(method)
        if hit is None:
            return None
        return Resolved(method, f"{pkg}.{type_name}.{method}", Path(hit[0]), hit[1], "function")

    def _type_pkg(self, raw: str, abs_path: str) -> tuple[str, str | None]:
        """Split a declared type into ``(packageQname, simpleName)``. ``*T`` -> ``T``; ``alias.T``
        resolves the alias via imports; a bare ``T`` is the file's own package."""
        raw = raw.lstrip("*")  # *T (pointer) -> T; the type field never carries the & operator
        if "." in raw:
            alias, _, simple = raw.partition(".")
            pkg = self._imports.get(abs_path, {}).get(alias)
            return (pkg, simple) if pkg is not None else ("", None)
        return self._file_pkg.get(abs_path, ""), raw


#: Go node types that open a new value scope (so a binding inside shadows package-level names).
_SCOPE_TYPES = frozenset({"function_declaration", "method_declaration", "func_literal"})


def _is_locally_shadowed(node: TSNode, name: str) -> bool:
    """Go binds names via a param/receiver/result or a local ``:=``/``var``/``const``/``range``
    declaration in an enclosing function scope; the shared skeleton walks the scopes."""
    return is_locally_shadowed(node, name, is_scope=_SCOPE_TYPES.__contains__, binds=_scope_binds)


def _scope_binds(scope: TSNode, name: str) -> bool:
    for field in ("receiver", "parameters", "result"):
        plist = scope.child_by_field_name(field)
        if plist is not None and _param_binds(plist, name):
            return True
    body = scope.child_by_field_name("body")
    if body is None:
        return False
    bindings: set[str] = set()
    _collect_local_bindings(body, bindings)
    return name in bindings


def _param_binds(plist: TSNode, name: str) -> bool:
    # A parameter/result NAME is a plain ``identifier`` child of a (variadic_)parameter_declaration;
    # the type is a ``type_identifier``/``pointer_type``/etc., so a type name is not a binding.
    for kind in ("parameter_declaration", "variadic_parameter_declaration"):
        for decl in _descend(plist, kind):
            if any(c.type == "identifier" and _text(c) == name for c in decl.children):
                return True
    return False


def _collect_local_bindings(node: TSNode, out: set[str]) -> None:
    """Names bound by ``:=``/``var``/``const``/``range`` in this scope; skips nested func bodies.

    Conservative on block scope: a binding inside a nested ``if``/``for``/``switch`` body is also
    collected, so a function-level reference can be treated as shadowed by an inner-block local of
    the same name. That is sound (never a wrong edge) but can drop a correct edge in that narrow
    case; the TypeScript guard is conservative the same way. Block-precise scoping is F-63.
    """
    for child in node.children:
        if child.type == "func_literal":
            continue  # a nested scope's bindings don't leak out
        if child.type in ("short_var_declaration", "range_clause"):
            left = child.child_by_field_name("left")
            if left is not None:
                for ident in _descend(left, "identifier"):
                    out.add(_text(ident))
            continue
        if child.type in ("var_spec", "const_spec"):
            for nm in child.children_by_field_name("name"):
                out.add(_text(nm))
            continue
        _collect_local_bindings(child, out)


def _declared_type(node: TSNode, name: str) -> str | None:
    """The type text declared for variable ``name`` (a param/receiver, or a ``var name T``) in the
    nearest enclosing function scope, or ``None``. A ``var`` counts only if it precedes the use
    (sound against a later same-named binding). Returns the raw type text (``T``/``*T``/``pkg.T``).
    Named return values (the ``result`` field) are intentionally not retrieved here — a sound miss.
    """
    before = node.start_point
    scope: TSNode | None = node.parent
    while scope is not None:
        if scope.type in _SCOPE_TYPES:
            for field in ("parameters", "receiver"):
                plist = scope.child_by_field_name(field)
                hit = _param_type(plist, name) if plist is not None else None
                if hit is not None:
                    return hit
        hit = _var_type(scope, name, before)
        if hit is not None:
            return hit
        scope = scope.parent
    return None


def _param_type(plist: TSNode, name: str) -> str | None:
    for decl in _descend(plist, "parameter_declaration"):
        type_node = decl.child_by_field_name("type")
        if type_node is not None and any(
            c.type == "identifier" and _text(c) == name for c in decl.children
        ):
            return _text(type_node)
    return None


def _var_type(scope: TSNode, name: str, before: tuple[int, int]) -> str | None:
    # Called on every ancestor (not only scope nodes): a `var` sits directly under a `block` or
    # the file root, neither of which is in _SCOPE_TYPES, so restricting to scopes would miss it.
    # `end_point <= before` (not start_point): the whole `var` statement must precede the use, so a
    # receiver inside the var's own initializer doesn't capture it (Go, like Rust, doesn't scope a
    # binding into its own initializer — `var x = f(x)` refers to an outer x).
    for child in scope.children:
        if child.type == "var_declaration" and child.end_point <= before:
            for spec in _descend(child, "var_spec"):
                type_node = spec.child_by_field_name("type")
                if type_node is not None and any(
                    c.type == "identifier" and _text(c) == name for c in spec.children
                ):
                    return _text(type_node)
    return None


def _receiver_type_name(receiver: TSNode | None) -> str | None:
    """The simple type name of a method receiver ``(r T)`` / ``(r *T)`` (always same-package)."""
    if receiver is None:
        return None
    # A Go receiver always carries exactly one parameter_declaration; the loop just handles the
    # (malformed) empty case by falling through to None.
    for decl in _descend(receiver, "parameter_declaration"):
        type_node = decl.child_by_field_name("type")
        if type_node is None:
            return None
        if type_node.type == "pointer_type":
            type_node = next((c for c in type_node.children if c.type == "type_identifier"), None)
        if type_node is not None and type_node.type == "type_identifier":
            return _text(type_node)
        return None
    return None


def _read_go_module(root: Path) -> str:
    """The module path from ``go.mod`` (``module github.com/org/repo``), dotted; else ``""``."""
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return ""
    for raw in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("module "):
            return line[len("module "):].strip().replace("/", ".")
    return ""


def _descend(node: TSNode, type_name: str) -> list[TSNode]:
    out: list[TSNode] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == type_name:
            out.append(cur)
        stack.extend(cur.children)
    return out


def _ancestor(node: TSNode, types: set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None
