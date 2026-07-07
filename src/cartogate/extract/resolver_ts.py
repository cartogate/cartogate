"""Pure-Python TypeScript name resolver (FUTURE F-54).

Implements the ``NameResolver`` protocol (``resolve(abs_path, line, column) -> Resolved | None``)
so the pipeline's edge construction is reused unchanged. It is **in-process, deterministic, and
air-gapped** — consistent with the jedi-over-subprocess choice — and **swappable** for a future
stack-graphs engine behind the same protocol.

Resolution is scope/import/path based, with **no type inference**: it binds imports (by relative-
path resolution), top-level function/class references and calls (by symbol table), and ``extends``
/``implements`` bases. A method call on an inferred-type receiver (``obj.method()`` without an
annotation) stays unresolved — soundly (never a *wrong* edge), which is the honest v1 ceiling.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser, Tree
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import is_locally_shadowed
from cartogate.extract.treesitter_util import text as _text

_TS_LANGUAGE = Language(tstypescript.language_typescript())
_CLASS_TYPES = frozenset(
    {"class_declaration", "abstract_class_declaration", "interface_declaration"}
)
_ARROW_VALUES = frozenset({"arrow_function", "function", "function_expression"})
_IDENT_TYPES = frozenset(
    {"identifier", "type_identifier", "property_identifier", "shorthand_property_identifier"}
)

#: Returned def_type for any resolved symbol — both "function" and "class" pass the pipeline's
#: linkable filter and route to symboldef_by_loc, so the precise kind is immaterial here.
_SYMBOL_TYPE = "function"
_MODULE_TYPE = "module"

#: A per-file import binding: (target file abs path | None, original name | None, is_external).
_Binding = tuple[str | None, str | None, bool]


#: Module-file extensions tried (in order) when resolving a relative import specifier.
_TS_MODULE_SUFFIXES = (".ts", ".tsx")


class TypeScriptResolver:
    """Resolves TypeScript name occurrences to their definitions, scope/import/path based.

    Subclassable for JavaScript (``resolver_js.py``): override the class attributes to swap the
    grammar, the import-path extensions, and to merge in CommonJS ``require`` bindings.
    """

    #: The tree-sitter grammar parsed (TS by default; the JS resolver uses the ``tsx`` grammar).
    _LANGUAGE: Language = _TS_LANGUAGE
    #: Extensions tried when resolving ``import './x'`` to an in-repo file.
    _MODULE_SUFFIXES: tuple[str, ...] = _TS_MODULE_SUFFIXES
    #: Whether to also bind CommonJS ``const x = require('./y')`` (JS only).
    _COMMONJS: bool = False

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        del project_root  # path resolution is relative to each importer; root is not needed
        source_set = set(sources)
        parser = Parser(self._LANGUAGE)
        self._trees: dict[str, Tree] = {
            p: parser.parse(c.encode("utf-8")) for p, c in sources.items()
        }
        # Top-level symbol name -> definition line, per file (matches the walker's start lines).
        self._symbols: dict[str, dict[str, int]] = {
            p: _top_level_symbols(t.root_node) for p, t in self._trees.items()
        }
        # Class/interface name -> {method -> def_line}, per file (F-69 typed-receiver resolution).
        self._members: dict[str, dict[str, dict[str, int]]] = {
            p: _class_members(t.root_node) for p, t in self._trees.items()
        }
        # Imported local name -> binding, per file (ESM, plus CommonJS for JS).
        self._imports: dict[str, dict[str, _Binding]] = {}
        for p, t in self._trees.items():
            bindings = _import_bindings(t.root_node, p, source_set, self._MODULE_SUFFIXES)
            if self._COMMONJS:
                for local, binding in _commonjs_bindings(
                    t.root_node, p, source_set, self._MODULE_SUFFIXES
                ).items():
                    bindings.setdefault(local, binding)  # an ESM import of the same name wins
            self._imports[p] = bindings

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        tree = self._trees.get(abs_path)
        if tree is None:
            return None
        node = tree.root_node.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None or node.type not in _IDENT_TYPES:
            return None
        name = _text(node)
        # x.method() — resolve when x has an explicitly DECLARED type in scope (F-69, sound: a
        # `x: Foo` annotation or a `const x = new Foo()`; no inference). The walker only emits a
        # NAME_CALL for a *called* member, so a bare property access never reaches this. Optional
        # chaining (`x?.m()`) is the same member_expression shape and is covered; a `Foo | null`
        # union annotation yields None from _type_name → unresolved (conservative).
        member = _method_call_member(node)
        if member is not None:
            return self._receiver_method(member, abs_path)
        # Soundness: if a parameter or local declaration in an enclosing function shadows this
        # name, it is NOT the top-level symbol — leave it unresolved rather than emit a wrong edge.
        if _is_locally_shadowed(node, name):
            return None
        return self._resolve_name(abs_path, name)

    def _receiver_method(self, member: TSNode, abs_path: str) -> Resolved | None:
        obj = member.child_by_field_name("object")
        prop = member.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier":
            return None  # only a simple `x.method()` receiver
        type_name = _declared_type(obj, _text(obj))
        return self._class_method(type_name, _text(prop), abs_path) if type_name else None

    def _class_method(self, class_name: str, method: str, abs_path: str) -> Resolved | None:
        """Look up ``class_name.method`` — a local class in ``abs_path`` or an imported one."""
        local = self._members.get(abs_path, {}).get(class_name)
        if local is not None:
            line = local.get(method)
            return Resolved(method, None, Path(abs_path), line, _SYMBOL_TYPE) if line else None
        binding = self._imports.get(abs_path, {}).get(class_name)
        if binding is not None:
            target, original, external = binding
            if not external and target is not None and original is not None:
                line = self._members.get(target, {}).get(original, {}).get(method)
                if line is not None:
                    return Resolved(method, None, Path(target), line, _SYMBOL_TYPE)
        return None

    def _resolve_name(self, abs_path: str, name: str) -> Resolved | None:
        binding = self._imports.get(abs_path, {}).get(name)
        if binding is not None:
            target, original, external = binding
            if external or target is None:
                return None  # bare specifier / unresolved module -> external (handled upstream)
            if original is not None:
                def_line = self._symbols.get(target, {}).get(original)
                if def_line is not None:
                    return Resolved(original, None, Path(target), def_line, _SYMBOL_TYPE)
            # default/namespace import, or a name not defined as a top-level symbol -> the module.
            return Resolved(name, None, Path(target), 1, _MODULE_TYPE)

        def_line = self._symbols.get(abs_path, {}).get(name)
        if def_line is not None:
            return Resolved(name, None, Path(abs_path), def_line, _SYMBOL_TYPE)
        return None


_SCOPE_TYPES = frozenset(
    {
        "function_declaration",
        "function",
        "function_expression",
        "arrow_function",
        "method_definition",
        "method_signature",
    }
)


_MEMBER_DEFS = frozenset({"method_definition", "method_signature"})


def _method_call_member(node: TSNode) -> TSNode | None:
    """If ``node`` is the property of a *called* member expression (the ``m`` in ``x.m()``), return
    that ``member_expression``; else ``None``."""
    if node.type != "property_identifier":
        return None
    member = node.parent
    if member is None or member.type != "member_expression":
        return None
    prop = member.child_by_field_name("property")
    if prop is None or prop.id != node.id:
        return None
    call = member.parent
    if call is not None and call.type == "call_expression":
        fn = call.child_by_field_name("function")
        if fn is not None and fn.id == member.id:
            return member
    return None


def _class_members(root: TSNode) -> dict[str, dict[str, int]]:
    """Map each **top-level** class/interface name to its ``{method -> def_line}`` (for typed-
    receiver lookup). Top-level only, mirroring ``_top_level_symbols``: a class nested in a function
    body is a *distinct* type per call yet shares its bare name, so indexing it would mis-attribute
    calls across functions — those receivers are left unresolved instead (sound). Accessors
    (``get``/``set``) are excluded: they are property reads/writes, not callable methods."""
    out: dict[str, dict[str, int]] = {}
    for child in root.named_children:
        decls = child.named_children if child.type == "export_statement" else (child,)
        for decl in decls:
            if decl.type not in _CLASS_TYPES:
                continue
            name = decl.child_by_field_name("name")
            body = decl.child_by_field_name("body")
            if name is None or body is None:
                continue
            methods: dict[str, int] = {}
            for member in body.children:
                if member.type in _MEMBER_DEFS and not _is_accessor(member):
                    mname = member.child_by_field_name("name")
                    if mname is not None and mname.type == "property_identifier":
                        methods.setdefault(_text(mname), member.start_point[0] + 1)
            out.setdefault(_text(name), methods)
    return out


def _is_accessor(method: TSNode) -> bool:
    """A getter/setter (``get x()`` / ``set x(v)``) — a property accessor, not a callable method."""
    return any(child.type in ("get", "set") for child in method.children)


def _declared_type(obj: TSNode, name: str) -> str | None:
    """The declared type name of receiver variable ``name`` in the nearest enclosing scope, or
    ``None``. Reads an *explicit* type only — a ``x: Foo`` annotation (param or ``const``/``let``)
    or a ``const x = new Foo()`` — never an inferred one. ``const``/``let`` can't be re-declared in
    a scope, so the nearest enclosing declaration is THE binding (no before-use subtlety needed)."""
    scope: TSNode | None = obj.parent
    while scope is not None:
        params = scope.child_by_field_name("parameters") or scope.child_by_field_name("parameter")
        if params is not None:
            hit = _param_type(params, name)
            if hit is not None:
                return hit
        hit = _block_decl_type(scope, name)
        if hit is not None:
            return hit
        scope = scope.parent
    return None


def _param_type(params: TSNode, name: str) -> str | None:
    for param in params.children:
        if param.type in ("required_parameter", "optional_parameter"):
            pat = param.child_by_field_name("pattern")
            if pat is not None and _text(pat) == name:
                return _type_name(param.child_by_field_name("type"))
    return None


def _block_decl_type(scope: TSNode, name: str) -> str | None:
    # Only `const`/`let` (lexical_declaration): they can't be re-declared in a scope and are scoped
    # into their own initializer (TDZ), so the single declaration in the nearest scope IS the
    # binding — no order/cutoff reasoning needed. `var` is intentionally excluded: it hoists and may
    # be re-declared with a different initializer, so picking one without flow analysis isn't sound.
    for child in scope.children:
        if child.type == "lexical_declaration":
            for decl in child.children:
                if decl.type != "variable_declarator":
                    continue
                nm = decl.child_by_field_name("name")
                if nm is None or nm.type != "identifier" or _text(nm) != name:
                    continue
                annotated = _type_name(decl.child_by_field_name("type"))
                if annotated is not None:
                    return annotated
                value = decl.child_by_field_name("value")  # const x = new Foo()
                if value is not None and value.type == "new_expression":
                    ctor = value.child_by_field_name("constructor")
                    if ctor is not None and ctor.type == "identifier":
                        return _text(ctor)
                return None
    return None


def _type_name(annotation: TSNode | None) -> str | None:
    """Simple type name from a ``type_annotation`` (``: Foo`` / ``: Foo<T>``); ``None`` for unions,
    qualified (``ns.Foo``), arrays, primitives — anything not a single in-repo nominal type."""
    if annotation is None:
        return None
    node = annotation.named_children[0] if annotation.named_children else None
    if node is None:
        return None
    if node.type == "type_identifier":
        return _text(node)
    if node.type == "generic_type":  # Foo<T> -> Foo
        base = node.child_by_field_name("name") or (
            node.named_children[0] if node.named_children else None
        )
        return _text(base) if base is not None and base.type == "type_identifier" else None
    return None


def _is_locally_shadowed(node: TSNode, name: str) -> bool:
    """TS binds names via params or local declarations in an enclosing function scope; the shared
    skeleton walks the scopes."""
    return is_locally_shadowed(node, name, is_scope=_SCOPE_TYPES.__contains__, binds=_scope_binds)


def _scope_binds(scope: TSNode, name: str) -> bool:
    params = scope.child_by_field_name("parameters") or scope.child_by_field_name("parameter")
    if params is not None and _subtree_has_identifier(params, name):
        return True
    body = scope.child_by_field_name("body")
    if body is None:
        return False
    bindings: set[str] = set()
    _collect_local_bindings(body, bindings)
    return name in bindings


def _subtree_has_identifier(node: TSNode, name: str) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "identifier" and _text(current) == name:
            return True
        stack.extend(current.children)
    return False


def _collect_local_bindings(node: TSNode, out: set[str]) -> None:
    """Names bound directly in a function body (vars + nested decl names); skips nested scopes."""
    for child in node.children:
        if child.type == "variable_declarator":
            name = child.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                out.add(_text(name))
            continue  # the initializer (e.g. an arrow) is a new scope — do not descend
        if child.type == "function_declaration" or child.type in _CLASS_TYPES:
            name = child.child_by_field_name("name")
            if name is not None:
                out.add(_text(name))  # the name binds here, but its body is a new scope
            continue
        if child.type in _SCOPE_TYPES:
            continue  # a nested scope's bindings don't leak into this one
        _collect_local_bindings(child, out)


def _top_level_symbols(root: TSNode) -> dict[str, int]:
    """Map each module-level function/class/interface/arrow-const name to its definition line."""
    out: dict[str, int] = {}
    for child in root.named_children:
        decls = child.named_children if child.type == "export_statement" else (child,)
        for decl in decls:
            _record_symbol(decl, out)
    return out


def _record_symbol(node: TSNode, out: dict[str, int]) -> None:
    if node.type == "function_declaration" or node.type in _CLASS_TYPES:
        name = node.child_by_field_name("name")
        if name is not None:
            out.setdefault(_text(name), node.start_point[0] + 1)  # first declaration wins
    elif node.type == "lexical_declaration":
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            value = decl.child_by_field_name("value")
            name = decl.child_by_field_name("name")
            if value is not None and value.type in _ARROW_VALUES and name is not None:
                out.setdefault(_text(name), decl.start_point[0] + 1)


def _import_bindings(
    root: TSNode, importer_abs: str, source_set: set[str], suffixes: tuple[str, ...]
) -> dict[str, _Binding]:
    out: dict[str, _Binding] = {}
    for child in root.named_children:
        if child.type != "import_statement":
            continue
        source_node = child.child_by_field_name("source")
        spec = _strip_quotes(_text(source_node)) if source_node is not None else ""
        target = _resolve_module(importer_abs, spec, source_set, suffixes)
        external = target is None
        clause = next((c for c in child.named_children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for member in clause.named_children:
            if member.type == "identifier":  # default import
                out.setdefault(_text(member), (target, None, external))
            elif member.type == "namespace_import":
                ident = next((c for c in member.named_children if c.type == "identifier"), None)
                if ident is not None:
                    out.setdefault(_text(ident), (target, None, external))
            elif member.type == "named_imports":
                for spec_node in member.named_children:
                    if spec_node.type != "import_specifier":
                        continue
                    name = spec_node.child_by_field_name("name")
                    local = spec_node.child_by_field_name("alias") or name
                    if local is not None and name is not None:
                        out.setdefault(_text(local), (target, _text(name), external))
    return out


def _resolve_module(
    importer_abs: str,
    spec: str,
    source_set: set[str],
    suffixes: tuple[str, ...] = _TS_MODULE_SUFFIXES,
) -> str | None:
    """Resolve a relative import specifier to an in-repo file path; bare specifiers → None.

    ``suffixes`` are the extensions tried in order (``.ts``/``.tsx`` for TS, ``.js``/``.jsx``/… for
    JS), as both a sibling file (``./x`` → ``x.ts``) and a package index (``./x`` → ``x/index.ts``).
    """
    if not (spec.startswith(".") or spec.startswith("/")):
        return None  # bare specifier (a package) — external
    base = (Path(importer_abs).parent / spec).resolve()
    candidates = [f"{base}{s}" for s in suffixes]
    candidates += [str(base / f"index{s}") for s in suffixes]
    candidates.append(str(base))  # already had an extension
    return next((c for c in candidates if c in source_set), None)


def _commonjs_bindings(
    root: TSNode, importer_abs: str, source_set: set[str], suffixes: tuple[str, ...]
) -> dict[str, _Binding]:
    """CommonJS (Node) ``require`` bindings: ``const X = require('./y')`` → the module; a
    destructure ``const {a, b} = require('./y')`` → per-name symbol bindings. Reuses the same
    relative-path resolution and ``_Binding`` shape, so ``_resolve_name`` handles them unchanged.
    """
    out: dict[str, _Binding] = {}
    for decl in _descend(root, "variable_declarator"):
        spec = _require_spec(decl.child_by_field_name("value"))
        if spec is None:
            continue
        target = _resolve_module(importer_abs, spec, source_set, suffixes)
        external = target is None
        name = decl.child_by_field_name("name")
        if name is None:
            continue
        if name.type == "identifier":  # const X = require('./y') -> the module
            out.setdefault(_text(name), (target, None, external))
        elif name.type == "object_pattern":  # const { a, b } = require('./y') -> named symbols
            for element in name.named_children:
                if element.type == "shorthand_property_identifier_pattern":
                    out.setdefault(_text(element), (target, _text(element), external))
    return out


def _require_spec(value: TSNode | None) -> str | None:
    """The string argument of a ``require("...")`` call, else ``None`` (dynamic/non-require)."""
    if value is None or value.type != "call_expression":
        return None
    fn = value.child_by_field_name("function")
    if fn is None or fn.type != "identifier" or _text(fn) != "require":
        return None
    args = value.child_by_field_name("arguments")
    if args is None:
        return None
    str_arg = next((c for c in args.named_children if c.type == "string"), None)
    return _strip_quotes(_text(str_arg)) if str_arg is not None else None


def _descend(root: TSNode, type_name: str) -> list[TSNode]:
    out: list[TSNode] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if current.type == type_name:
            out.append(current)
        stack.extend(current.children)
    return out


def _strip_quotes(text: str) -> str:
    return text.strip().strip("\"'`")
