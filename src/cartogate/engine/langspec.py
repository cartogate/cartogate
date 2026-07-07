"""Per-language wiring for the CFG/PDG/slicing stack (F-03, F-08).

Each :class:`SliceLang` bundles what the language-agnostic CFG/PDG/slicer needs to run on a real
file: the tree-sitter parser, the function-definition node types, the language's :class:`CfgSpec`,
its def/use extractor + parameter binder, and the node types that make a statement an *observable
output* (for the slicing-based LOCALIZE refinement). This is the single place that knows "how to
parse, find functions, and read def/use" for a language, so everything above stays grammar-neutral.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath

import tree_sitter_cpp as tscpp
import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser

from cartogate.engine import def_use as _du_py
from cartogate.engine import def_use_c as _du_c
from cartogate.engine import def_use_go as _du_go
from cartogate.engine import def_use_java as _du_java
from cartogate.engine import def_use_js as _du_js
from cartogate.engine.cfg import C_CFG, GO_CFG, JAVA_CFG, JS_CFG, PY_CFG, CfgSpec
from cartogate.engine.def_use import DefUse
from cartogate.schema.enums import Language


@dataclass(frozen=True, slots=True)
class SliceLang:
    """Everything the CFG/PDG/slicing stack needs to analyse one language from source."""

    language: Language
    parser: Parser
    func_types: frozenset[str]  # function-definition node types (each analysed independently)
    cfg: CfgSpec
    def_use_for_node: Callable[[TSNode, str], DefUse]
    parameter_defs: Callable[[TSNode], frozenset[str]]
    #: Statement/expression node types whose presence makes a statement an observable output
    #: (return/raise/throw/yield + a call), used by ``observable_output_nodes`` for LOCALIZE.
    output_types: frozenset[str]
    call_types: frozenset[str]
    #: Nested-scope node types not descended into when scanning a statement for observable outputs.
    opaque_scopes: frozenset[str]
    #: Whether plain-name writes to ``global``/``nonlocal``-declared vars are observable (Python).
    detect_global_writes: bool = False
    body_field: str = "body"  # the field holding a function's body block


#: Python parser, CFG spec, and def/use.
_PY = SliceLang(
    language=Language.PYTHON,
    parser=Parser(TSLanguage(tspython.language())),
    func_types=frozenset({"function_definition"}),
    cfg=PY_CFG,
    def_use_for_node=_du_py.def_use_for_node,
    parameter_defs=_du_py.parameter_defs,
    output_types=frozenset({"return_statement", "raise_statement", "yield"}),
    call_types=frozenset({"call"}),
    opaque_scopes=frozenset({"function_definition", "lambda", "class_definition"}),
    detect_global_writes=True,
)

#: JS and TS share the ``tsx`` grammar (a JS/JSX superset), so one SliceLang serves both.
_JS = SliceLang(
    language=Language.TYPESCRIPT,
    parser=Parser(TSLanguage(tstypescript.language_tsx())),
    func_types=frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "function_expression",
            "generator_function",
            "arrow_function",
            "method_definition",
        }
    ),
    cfg=JS_CFG,
    def_use_for_node=_du_js.def_use_for_node,
    parameter_defs=_du_js.parameter_defs,
    output_types=frozenset({"return_statement", "throw_statement", "yield_expression"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    opaque_scopes=_du_js.JS_SCOPE_TYPES,
    detect_global_writes=False,
)

#: Go: ``function_declaration``/``method_declaration``; ``panic`` is a call (no raise/throw);
#: a single ``for`` (all loop forms); ``switch``/``select``/``defer``/``go`` are opaque.
_GO = SliceLang(
    language=Language.GO,
    parser=Parser(TSLanguage(tsgo.language())),
    func_types=frozenset({"function_declaration", "method_declaration", "func_literal"}),
    cfg=GO_CFG,
    def_use_for_node=_du_go.def_use_for_node,
    parameter_defs=_du_go.parameter_defs,
    output_types=frozenset({"return_statement"}),
    call_types=frozenset({"call_expression"}),
    opaque_scopes=_du_go.GO_SCOPE_TYPES,
    detect_global_writes=False,
)

#: Java: ``method_declaration``/``constructor_declaration``; ``throw`` not ``raise``; C-style +
#: enhanced-for + while + do loops; ``try``/``switch``/``synchronized`` opaque.
_JAVA = SliceLang(
    language=Language.JAVA,
    parser=Parser(TSLanguage(tsjava.language())),
    func_types=frozenset({"method_declaration", "constructor_declaration", "lambda_expression"}),
    cfg=JAVA_CFG,
    def_use_for_node=_du_java.def_use_for_node,
    parameter_defs=_du_java.parameter_defs,
    output_types=frozenset({"return_statement", "throw_statement"}),
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
    opaque_scopes=_du_java.JAVA_SCOPE_TYPES,
    detect_global_writes=False,
)

#: C/C++: one SliceLang on the ``cpp`` grammar (a C superset) serves both. ``compound_statement``
#: bodies; ``return``/``throw``; C-style + range-for + while + do loops; switch/try opaque.
_C = SliceLang(
    language=Language.CPP,
    parser=Parser(TSLanguage(tscpp.language())),
    func_types=frozenset({"function_definition", "lambda_expression"}),
    cfg=C_CFG,
    def_use_for_node=_du_c.def_use_for_node,
    parameter_defs=_du_c.parameter_defs,
    output_types=frozenset({"return_statement", "throw_statement"}),
    call_types=frozenset({"call_expression", "new_expression"}),
    opaque_scopes=_du_c.C_SCOPE_TYPES,
    detect_global_writes=False,
)

_BY_SUFFIX: dict[str, SliceLang] = {
    ".py": _PY,
    ".ts": _JS,
    ".tsx": _JS,
    ".js": _JS,
    ".jsx": _JS,
    ".mjs": _JS,
    ".cjs": _JS,
    ".go": _GO,
    ".java": _JAVA,
    ".c": _C,
    ".h": _C,
    ".cpp": _C,
    ".cc": _C,
    ".cxx": _C,
    ".hpp": _C,
    ".hh": _C,
    ".hxx": _C,
}

#: Suffixes the CFG/slicing stack can analyse (a subset of all indexed languages, growing per F-08).
SUPPORTED_SUFFIXES: frozenset[str] = frozenset(_BY_SUFFIX)

PYTHON = _PY  # convenience for the Python-only callers (localize, the default everywhere)


_BY_NAME: dict[str, SliceLang] = {
    "python": _PY,
    "py": _PY,
    "typescript": _JS,
    "ts": _JS,
    "tsx": _JS,
    "javascript": _JS,
    "js": _JS,
    "jsx": _JS,
    "go": _GO,
    "golang": _GO,
    "java": _JAVA,
    "c": _C,
    "cpp": _C,
    "c++": _C,
    "cxx": _C,
}


def lang_for_path(path: PurePath) -> SliceLang | None:
    """The :class:`SliceLang` for ``path``'s suffix, or ``None`` if slicing can't handle it yet."""
    return _BY_SUFFIX.get(path.suffix.lower())


def lang_for_name(name: str) -> SliceLang | None:
    """The :class:`SliceLang` for a language name (``python``/``typescript``/``javascript``…)."""
    return _BY_NAME.get(name.lower())


def functions_in(root: TSNode, lang: SliceLang) -> list[TSNode]:
    """Every function-definition node under ``root`` (DFS; nested functions included — each is
    analysed independently, a nested one treated as opaque fall-through by its encloser)."""
    out: list[TSNode] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type in lang.func_types:
            out.append(cur)
        stack.extend(cur.named_children)  # function-definition nodes are always named
    return out


def function_at(source: bytes, line: int, lang: SliceLang = _PY) -> TSNode | None:
    """The function-definition node starting at ``line``, else the innermost one containing it, else
    ``None`` — parsed with ``lang``'s grammar (default Python)."""
    root = lang.parser.parse(source).root_node
    funcs = functions_in(root, lang)
    exact = [f for f in funcs if f.start_point[0] + 1 == line]
    if exact:
        return exact[0]
    containing = [f for f in funcs if f.start_point[0] + 1 <= line <= f.end_point[0] + 1]
    return min(containing, key=lambda f: f.end_point[0] - f.start_point[0], default=None)
