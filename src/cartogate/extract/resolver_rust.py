"""Pure-Python Rust name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for Rust. Rust's
explicit module paths (``crate::``/``self::``/``super::``), explicit ``use`` imports, and
``impl`` blocks that name their type make sound resolution tractable without a compiler:

- **use imports / paths** (``crate::models::User``) → the in-repo symbol/module, else external.
- **same-module / crate-root calls** (``validate()``) → the function in scope (shadow-guarded).
- **associated calls** (``User::new()``) → the method, when ``User`` resolves to an in-repo type.
- **type references** and ``impl Trait for Type`` → the in-repo type/trait.
- **typed-receiver method calls** (``x.method()``) → the method, when ``x`` has an explicitly
  *declared* type in scope (a ``let x: T`` annotation, a ``let x = T {..}`` literal, or a param).

Honest ceiling (returns ``None`` → no edge): a method call whose receiver type is *inferred*
(``let x = make()`` / ``T::new()``), names bound to a local (shadowing), and anything outside crate.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import is_locally_shadowed
from cartogate.extract.treesitter_util import text as _text

_RUST_LANGUAGE = Language(tsrust.language())
_TYPE_DECLS = frozenset({"struct_item", "enum_item", "union_item", "trait_item", "type_item"})
_FN_DECLS = frozenset({"function_item", "function_signature_item"})
_INDEX_STEMS = frozenset({"mod", "lib", "main"})
#: Scopes that can bind a shadowing local/param, and the nodes that bind names within them.
_SCOPES = frozenset({"block", "function_item", "closure_expression"})


class RustResolver:
    """Resolves Rust name occurrences against an in-repo crate symbol/module index."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_RUST_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._file_module: dict[str, str] = {}
        self._symbols: dict[str, tuple[str, int]] = {}  # dotted FQN -> (abs, def_line)
        self._modules: dict[str, str] = {}  # module qname -> one abs file (for module targets)
        self._imports: dict[str, dict[str, str]] = {}  # abs -> local name -> dotted FQN (in-repo)
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)
        for abs_path, text in sources.items():
            self._index_imports(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        module = self._module_of(abs_path)
        self._file_module[abs_path] = module
        self._modules.setdefault(module, abs_path)
        self._index_items(root, module, abs_path)

    def _index_items(self, container_node: TSNode, module: str, abs_path: str) -> None:
        for item in container_node.children:
            if item.type in _FN_DECLS or item.type in _TYPE_DECLS:
                name = item.child_by_field_name("name")
                if name is not None:
                    self._symbols.setdefault(
                        _join(module, _text(name)), (abs_path, item.start_point[0] + 1)
                    )
            elif item.type == "mod_item":  # inline module → nest
                name = item.child_by_field_name("name")
                body = item.child_by_field_name("body")
                if name is not None and body is not None:
                    sub = _join(module, _text(name))
                    self._modules.setdefault(sub, abs_path)
                    self._index_items(body, sub, abs_path)
            elif item.type == "impl_item":  # methods → under the impl's type
                type_node = item.child_by_field_name("type")
                body = item.child_by_field_name("body")
                if type_node is not None and body is not None:
                    type_fqn = _join(module, _text(type_node))
                    self._index_items(body, type_fqn, abs_path)
            elif item.type == "declaration_list":  # mod/impl body wrapper, if reached directly
                self._index_items(item, module, abs_path)

    def _index_imports(self, abs_path: str, text: str) -> None:
        self._imports[abs_path] = {}
        module = self._file_module.get(abs_path, "")
        for use_decl in _descend(self._trees[abs_path], "use_declaration"):
            for path, local in _use_bindings(use_decl):
                fqn = self._path_to_fqn(path.split("::"), module, abs_path, follow_imports=False)
                if fqn is not None:
                    self._imports[abs_path][local] = fqn

    def _module_of(self, abs_path: str) -> str:
        try:
            rel = Path(abs_path).resolve().relative_to(self._root)
        except ValueError:
            return ""
        parts = list(rel.parts)
        parts[-1] = parts[-1].removesuffix(".rs")
        if parts and parts[-1] in _INDEX_STEMS:
            parts = parts[:-1]
        # Crate-root prefix mirrors the pipeline's ``module_qname`` (``root_module="crate"``), so
        # ``lib.rs`` -> ``crate`` and ``crate::x`` paths line up with derived qnames.
        return ".".join(["crate", *parts])

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None
        module = self._file_module.get(abs_path, "")

        if _ancestor(node, {"use_declaration"}) is not None:
            scoped = _ancestor(node, {"scoped_identifier"}) or node
            return self._lookup(self._path_to_fqn(_text(scoped).split("::"), module, abs_path))

        call = _ancestor(node, {"call_expression"})
        if call is not None and _within(call.child_by_field_name("function"), node):
            fn = call.child_by_field_name("function")
            if fn is not None and fn.type == "field_expression":
                # x.method() — resolve when x has an explicitly DECLARED type in scope (F-69,
                # sound): a `let x: T`, a `let x = T {..}` literal, or a param `x: T`. No inference.
                recv = fn.child_by_field_name("value")
                field = fn.child_by_field_name("field")
                if recv is None or field is None or recv.type != "identifier":
                    return None
                segs = _declared_type(recv, _text(recv))
                if segs is None:
                    return None  # inferred / undeclared receiver type — sound ceiling
                type_fqn = self._path_to_fqn(segs, module, abs_path)
                return self._lookup(_join(type_fqn, _text(field))) if type_fqn else None
            if fn is None:
                return None  # value-receiver method call — sound ceiling
            segs = _text(fn).split("::")
            if len(segs) == 1 and self._is_shadowed(node, segs[0]):
                return None
            return self._lookup(self._path_to_fqn(segs, module, abs_path))

        # A type position (reference / impl trait / field / param / return type).
        scoped_ty = _ancestor(node, {"scoped_type_identifier"})
        segs = _text(scoped_ty).split("::") if scoped_ty is not None else [_text(node)]
        return self._lookup(self._path_to_fqn(segs, module, abs_path))

    def _path_to_fqn(
        self, segs: list[str], module: str, abs_path: str, *, follow_imports: bool = True
    ) -> str | None:
        segs = [s for s in segs if s]
        if not segs:
            return None
        head, *rest = segs
        if head == "crate":
            return _join("crate", ".".join(rest))
        if head == "self":
            return _join(module, ".".join(rest))
        if head == "super":
            return _join(_parent(module), ".".join(rest))
        if follow_imports and head in self._imports.get(abs_path, {}):
            return _join(self._imports[abs_path][head], ".".join(rest))
        if not rest:  # a bare name: prefer the current module, then the crate root
            for candidate in (_join(module, head), _join("crate", head)):
                if candidate in self._symbols or candidate in self._modules:
                    return candidate
            return _join(module, head)
        return ".".join(segs)  # crate-relative path (Rust 2018) or external

    def _lookup(self, fqn: str | None) -> Resolved | None:
        if fqn is None:
            return None
        hit = self._symbols.get(fqn)
        if hit is not None:
            return Resolved(fqn.rsplit(".", 1)[-1], fqn, Path(hit[0]), hit[1], "class")
        mod_file = self._modules.get(fqn)
        if mod_file is not None:
            return Resolved(fqn.rsplit(".", 1)[-1], fqn, Path(mod_file), None, "module")
        return None

    def _is_shadowed(self, node: TSNode, name: str) -> bool:
        return is_locally_shadowed(node, name, is_scope=_SCOPES.__contains__, binds=_binds)


def _binds(scope: TSNode, name: str) -> bool:
    """Whether ``scope`` binds ``name`` as a ``let`` local or a function/closure parameter."""
    for child in _descend(scope, None):
        if child.type in ("let_declaration", "parameter"):
            pat = child.child_by_field_name("pattern")
            if pat is not None and _text(pat) == name:
                return True
    return False


def _declared_type(recv: TSNode, name: str) -> list[str] | None:
    """The declared type of receiver variable ``name`` as path segments, or ``None``.

    Reads an *explicit* type only — a ``let x: T`` annotation, a ``let x = T {..}`` struct literal,
    or a ``x: T`` parameter — so it never infers a function's return type (``let x = make()`` stays
    unresolved). Walks scopes call-site-outward; within a block the nearest ``let`` *before* the use
    wins (Rust allows free re-binding via shadowing), keeping the result sound.
    """
    before = recv.start_point
    node: TSNode | None = recv.parent
    while node is not None:
        if node.type == "block":
            hit = _block_let_type(node, name, before)
            if hit is not None:
                return hit
        params = node.child_by_field_name("parameters")
        if params is not None:
            hit = _param_type(params, name)
            if hit is not None:
                return hit
        node = node.parent
    return None


def _block_let_type(block: TSNode, name: str, before: tuple[int, int]) -> list[str] | None:
    """Type of the nearest `let name` in this block whose *whole* statement precedes the use, or
    ``None``. ``end_point <= before`` (not ``start_point``) so a receiver *inside* a let's own
    initializer doesn't capture that let — Rust doesn't scope a binding into its own initializer
    (``let x = f(x)`` refers to the outer ``x``), so the nearest binding wins soundly."""
    found: TSNode | None = None
    for child in block.children:  # in source order → the last match before the use is the nearest
        if child.type == "let_declaration" and child.end_point <= before:
            pat = child.child_by_field_name("pattern")
            if pat is not None and _text(pat) == name:
                found = child
    return _let_type(found) if found is not None else None


def _param_type(params: TSNode, name: str) -> list[str] | None:
    """Type segments of the parameter named ``name`` in a parameter list, or ``None``."""
    for param in params.children:
        if param.type == "parameter":
            pat = param.child_by_field_name("pattern")
            if pat is not None and _text(pat) == name:
                return _type_segs(param.child_by_field_name("type"))
    return None


def _let_type(let_node: TSNode) -> list[str] | None:
    """Type segments of a ``let`` binding from an explicit source only — a ``: T`` annotation or a
    ``T {..}`` struct-literal value. ``None`` for any other initializer (the type would be inferred,
    which isn't sound: e.g. ``let x = T::new()`` — ``new`` need not return ``T``)."""
    annotation = let_node.child_by_field_name("type")
    if annotation is not None:  # let x: T = …
        return _type_segs(annotation)
    value = let_node.child_by_field_name("value")
    if value is not None and value.type == "struct_expression":  # let x = T { .. }
        return _type_segs(value.child_by_field_name("name"))
    return None  # let x = expr — the type would be inferred (not sound)


def _type_segs(type_node: TSNode | None) -> list[str] | None:
    """Path segments of a type, unwrapping references and generics; ``None`` for non-nominal types
    (tuples, primitives, slices, …) that can't carry an in-repo method."""
    if type_node is None:
        return None
    if type_node.type == "reference_type":  # &T / &mut T (auto-deref receiver)
        return _type_segs(type_node.child_by_field_name("type"))
    if type_node.type == "generic_type":  # Vec<T> -> base
        return _type_segs(type_node.child_by_field_name("type"))
    if type_node.type == "type_identifier":
        return [_text(type_node)]
    if type_node.type == "scoped_type_identifier":  # a::B::C
        return _text(type_node).split("::")
    return None


def _descend(node: TSNode, type_name: str | None) -> list[TSNode]:
    out: list[TSNode] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if type_name is None or cur.type == type_name:
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


def _within(container: TSNode | None, node: TSNode) -> bool:
    if container is None:
        return False
    cur: TSNode | None = node
    while cur is not None:
        if cur == container:
            return True
        cur = cur.parent
    return False


def _use_bindings(use_decl: TSNode) -> list[tuple[str, str]]:
    """Flatten a ``use`` into ``(full_path, local_name)`` pairs (handles ``{A, B}`` + ``as``)."""
    out: list[tuple[str, str]] = []

    def walk(arg: TSNode, prefix: str) -> None:
        if arg.type in ("scoped_identifier", "identifier"):
            path = _join2(prefix, _text(arg))
            out.append((path, path.rsplit("::", 1)[-1]))
        elif arg.type == "use_as_clause":
            path_node = arg.child_by_field_name("path")
            alias = arg.child_by_field_name("alias")
            if path_node is not None and alias is not None:
                out.append((_join2(prefix, _text(path_node)), _text(alias)))
        elif arg.type == "scoped_use_list":
            base = arg.child_by_field_name("path")
            base_path = _join2(prefix, _text(base)) if base is not None else prefix
            lst = arg.child_by_field_name("list")
            if lst is not None:
                for item in lst.named_children:
                    walk(item, base_path)
        elif arg.type == "use_list":
            for item in arg.named_children:
                walk(item, prefix)

    arg = use_decl.child_by_field_name("argument")
    if arg is not None:
        walk(arg, "")
    return out


def _join(prefix: str, suffix: str) -> str:
    """Join dotted qnames, dropping empty segments."""
    return ".".join(p for p in (prefix, suffix) if p)


def _join2(prefix: str, suffix: str) -> str:
    """Join ``::`` path segments."""
    return f"{prefix}::{suffix}" if prefix else suffix


def _parent(module: str) -> str:
    return module.rsplit(".", 1)[0] if "." in module else ""
