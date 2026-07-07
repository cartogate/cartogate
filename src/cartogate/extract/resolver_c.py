"""Pure-Python C name resolver (F-08) — in-process, deterministic, air-gapped.

Implements the :class:`~cartogate.extract.resolver.NameResolver` protocol for C. C has no
namespaces and no overloading, and external functions have **global linkage**, so resolution is a
name index, not a scope walk:

- **functions** — a call ``f(...)`` binds to the in-repo *definition* of ``f``: the same file's
  definition first (covers ``static`` file-local functions), then the unique external (non-static)
  definition anywhere in the repo. A name with no in-repo definition (libc, a function pointer, a
  macro) stays unresolved — no wrong edge.
- **types** — a ``struct T`` / typedef-name use binds to the unique in-repo definition of that
  type (ambiguous duplicates → unresolved).
- **includes** — ``#include "x.h"`` resolves to the in-repo header *module* (relative to the
  including file); ``#include <x.h>`` is a system header → external.

There is no compiler, no preprocessor expansion, and no macro evaluation.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_c as tsc
from tree_sitter import Language, Parser
from tree_sitter import Node as TSNode

from cartogate.extract.resolver import Resolved
from cartogate.extract.treesitter_util import text as _text

_C_LANGUAGE = Language(tsc.language())
_TYPE_SPECIFIERS = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})

#: Sentinel marking a name defined more than once (ambiguous) — left unresolved (no wrong edge).
_AMBIGUOUS = ("", -1)


class CResolver:
    """Resolves C name occurrences against in-repo function/type/include indexes."""

    def __init__(self, project_root: Path, sources: dict[str, str]) -> None:
        self._root = project_root.resolve()
        self._parser = Parser(_C_LANGUAGE)
        self._trees: dict[str, TSNode] = {}
        self._func_global: dict[str, tuple[str, int]] = {}  # name -> (abs, line); external linkage
        self._func_local: dict[str, dict[str, tuple[str, int]]] = {}  # abs -> name -> (abs, line)
        self._types: dict[str, tuple[str, int]] = {}  # name -> (abs, line); unique-or-ambiguous
        self._abspaths = {str(Path(p).resolve()) for p in sources}
        for abs_path, text in sources.items():
            self._index_file(str(Path(abs_path).resolve()), text)

    # --- indexing ----------------------------------------------------------- #

    def _index_file(self, abs_path: str, text: str) -> None:
        root = self._parser.parse(text.encode("utf-8")).root_node
        self._trees[abs_path] = root
        self._func_local[abs_path] = {}
        self._walk_defs(root, abs_path)

    def _walk_defs(self, node: TSNode, abs_path: str) -> None:
        for child in node.children:
            if child.type == "function_definition":
                self._index_function(child, abs_path)
            elif child.type == "type_definition":
                self._index_typedef(child, abs_path)
            elif child.type in _TYPE_SPECIFIERS and child.child_by_field_name("body") is not None:
                self._index_type_name(child.child_by_field_name("name"), abs_path)
            else:
                self._walk_defs(child, abs_path)  # descend (e.g. into a `declaration` wrapper)

    def _index_function(self, node: TSNode, abs_path: str) -> None:
        name_node, _ = _declarator_name(node.child_by_field_name("declarator"))
        if name_node is None:
            return
        name = _text(name_node)
        line = node.start_point[0] + 1
        self._func_local[abs_path][name] = (abs_path, line)
        is_static = any(
            c.type == "storage_class_specifier" and _text(c) == "static" for c in node.children
        )
        if not is_static:  # external linkage -> the global index (ambiguous if redefined)
            self._func_global[name] = (
                _AMBIGUOUS if name in self._func_global else (abs_path, line)
            )

    def _index_typedef(self, node: TSNode, abs_path: str) -> None:
        name_node = node.child_by_field_name("declarator")
        while name_node is not None and name_node.type != "type_identifier":
            name_node = name_node.child_by_field_name("declarator") or _first_named(name_node)
        self._index_type_name(name_node, abs_path)

    def _index_type_name(self, name_node: TSNode | None, abs_path: str) -> None:
        if name_node is None:
            return
        name = _text(name_node)
        line = name_node.start_point[0] + 1
        self._types[name] = _AMBIGUOUS if name in self._types else (abs_path, line)

    # --- resolution --------------------------------------------------------- #

    def resolve(self, abs_path: str, line: int, column: int) -> Resolved | None:
        abs_path = str(Path(abs_path).resolve())
        root = self._trees.get(abs_path)
        if root is None:
            return None
        node = root.descendant_for_point_range((line - 1, column), (line - 1, column))
        if node is None:
            return None

        include = _ancestor(node, {"preproc_include"})
        if include is not None:
            return self._resolve_include(include, abs_path)

        call = _ancestor(node, {"call_expression"})
        if call is not None and _is_call_name(call, node):
            return self._resolve_call(_text(node), abs_path)

        if node.type == "type_identifier":
            return self._resolve_type(_text(node))
        return None

    def _resolve_include(self, include: TSNode, abs_path: str) -> Resolved | None:
        path = include.child_by_field_name("path")
        if path is None or path.type != "string_literal":  # <system> headers -> external
            return None
        inner = next((c for c in path.children if c.type == "string_content"), None)
        rel = _text(inner) if inner is not None else _text(path).strip('"')
        target = (Path(abs_path).parent / rel).resolve()
        if str(target) in self._abspaths:
            return Resolved(target.name, None, target, 1, "module")
        return None

    def _resolve_call(self, name: str, abs_path: str) -> Resolved | None:
        hit = self._func_local.get(abs_path, {}).get(name)  # same file first (covers `static`)
        if hit is None:
            hit = self._func_global.get(name)
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(name, name, Path(hit[0]), hit[1], "function")

    def _resolve_type(self, name: str) -> Resolved | None:
        hit = self._types.get(name)
        if hit is None or hit == _AMBIGUOUS:
            return None
        return Resolved(name, name, Path(hit[0]), hit[1], "class")


# --- helpers ---------------------------------------------------------------- #


def _declarator_name(decl: TSNode | None) -> tuple[TSNode | None, TSNode | None]:
    node = decl
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    if node is None:
        return None, None
    name = node.child_by_field_name("declarator")
    while name is not None and name.type not in ("identifier", "field_identifier"):
        name = name.child_by_field_name("declarator") or _first_named(name)
    return name, node.child_by_field_name("parameters")


def _ancestor(node: TSNode, types: frozenset[str] | set[str]) -> TSNode | None:
    cur: TSNode | None = node
    while cur is not None:
        if cur.type in types:
            return cur
        cur = cur.parent
    return None


def _is_call_name(call: TSNode, node: TSNode) -> bool:
    func = call.child_by_field_name("function")
    return func is not None and func.type == "identifier" and func.start_point == node.start_point


def _first_named(node: TSNode) -> TSNode | None:
    return next((c for c in node.children if c.is_named), None)
