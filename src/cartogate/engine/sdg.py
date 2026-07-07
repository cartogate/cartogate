"""Interprocedural backward program slicing — a context-insensitive system dependence graph (F-03).

Answers "what code, **across function calls**, affects this line": the intraprocedural backward
slice of the seed function, then — for each call *within that slice* — the callee's own backward
slice from its observable outputs, transitively, bounded by depth. Call targets are taken from the
EXTRACTED call graph, matched to the slice by each resolved edge's call-site line (alias-robust).

Context-insensitive and **over-approximating** — it inherits the intraprocedural slicer's superset
property, so an interprocedural slice is a superset of the true one, never a subset (modulo the
accepted EXTRACTED-call-edge baseline). Advisory, out of the gate (R7); each callee is sliced in its
own file's language (Python or JS/TS), lazy/in-memory (its PDG built on demand from source).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from cartogate.engine.langspec import SliceLang, function_at, lang_for_path
from cartogate.engine.pdg import ProgramDependenceGraph, build_pdg, observable_output_nodes
from cartogate.schema.enums import Confidence, EdgeType, NodeKind
from cartogate.schema.nodes import Node
from cartogate.store.base import Direction, StoreInterface

ReadSource = Callable[[str], bytes | None]

_CALL_EDGES = frozenset({EdgeType.CALLS})
DEFAULT_DEPTH = 3  # how many call hops out from the seed to expand


@dataclass(frozen=True, slots=True)
class FunctionSlice:
    """One function's contribution to an interprocedural slice."""

    qualified_name: str
    path: str
    is_seed: bool
    statements: tuple[tuple[int, str], ...]  # (line, code), sorted by line

    @property
    def lines(self) -> tuple[int, ...]:
        return tuple(line for line, _ in self.statements)


@dataclass(frozen=True, slots=True)
class InterprocSlice:
    """A backward interprocedural slice: the seed function + the called functions that affect it."""

    seed: str  # path:line
    depth: int
    functions: tuple[FunctionSlice, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "depth": self.depth,
            "functions": [
                {
                    "qualified_name": f.qualified_name,
                    "path": f.path,
                    "is_seed": f.is_seed,
                    "lines": list(f.lines),
                    "statements": [{"line": ln, "code": code} for ln, code in f.statements],
                }
                for f in self.functions
            ],
            "function_count": len(self.functions),
        }

    def to_markdown(self) -> str:
        head = (
            f"## Cartogate interprocedural slice: `{self.seed}` (backward, depth {self.depth})\n\n"
            f"{len(self.functions)} function(s) affect this line:\n"
        )
        blocks = []
        for f in self.functions:
            tag = " (seed)" if f.is_seed else ""
            body = "\n".join(f"- {ln}: `{code}`" for ln, code in f.statements)
            blocks.append(f"\n### `{f.qualified_name}` — {f.path}{tag}\n\n{body}")
        return head + "\n".join(blocks) + "\n"


def _statements(pdg: ProgramDependenceGraph, node_ids: set[int]) -> tuple[tuple[int, str], ...]:
    """Render a slice (node ids) as sorted ``(line, code)`` pairs."""
    rendered = pdg.to_dict(node_ids)["statements"]
    return tuple((s["line"], s["code"]) for s in rendered)


def _lang_for(path: str) -> SliceLang | None:
    """The slicing language for a (repo-relative POSIX) store path, or ``None`` if unsupported."""
    return lang_for_path(PurePosixPath(path))


def _symbol_at(store: StoreInterface, path: str, line: int) -> Node | None:
    """The innermost visible symbol (in a slicing-supported language) whose span covers
    ``(path, line)``."""
    best: Node | None = None
    for node_id in store.visible_node_ids():
        node = store.get_node(node_id)
        if node is None or node.kind is not NodeKind.SYMBOL:
            continue
        if _lang_for(node.location.path) is None:
            continue
        loc = node.location
        span = loc.end_line - loc.start_line
        covers = loc.path == path and loc.start_line <= line <= loc.end_line
        if covers and (best is None or span < best.location.end_line - best.location.start_line):
            best = node
    return best


def _callees_in_slice(store: StoreInterface, symbol: Node, lines: set[int]) -> list[Node]:
    """Direct callees of ``symbol`` (in a slicing-supported language) whose call site (the EXTRACTED
    CALLS edge's ``source_location.line``) falls on a sliced line. Matching by the *resolved edge's
    line* (not by re-deriving the called name) makes it robust to import aliases and multi-line
    calls."""
    out: list[Node] = []
    seen: set[str] = set()
    for edge in store.neighbors(
        symbol.id,
        edge_types=_CALL_EDGES,
        direction=Direction.OUT,
        confidence=(Confidence.EXTRACTED,),
    ):
        loc = edge.source_location
        if loc is None or loc.line not in lines or edge.dst in seen:
            continue
        callee = store.get_node(edge.dst)
        if (
            callee is None
            or callee.kind is not NodeKind.SYMBOL
            or _lang_for(callee.location.path) is None
        ):
            continue
        seen.add(edge.dst)
        out.append(callee)
    return out


def _output_slice(
    read_source: ReadSource, symbol: Node
) -> tuple[tuple[tuple[int, str], ...], set[int]] | None:
    """A callee's backward slice from its observable outputs: the part that determines what it
    returns or does. Returns ``(statements, sliced-lines)`` or ``None`` to skip."""
    lang = _lang_for(symbol.location.path)
    source = read_source(symbol.location.path)
    if lang is None or source is None:
        return None
    func = function_at(source, symbol.location.start_line, lang)
    if func is None or func.child_by_field_name(lang.body_field) is None:
        return None
    pdg = build_pdg(func, lang)
    seeds = observable_output_nodes(pdg, lang)
    node_ids = pdg.backward_slice(seeds) if seeds else set(pdg.cfg.ts_nodes)
    sliced_lines = {pdg.cfg.nodes[n].start_line for n in node_ids if n in pdg.cfg.nodes}
    return _statements(pdg, node_ids), sliced_lines


def interprocedural_backward_slice(
    store: StoreInterface,
    read_source: ReadSource,
    seed_path: str,
    seed_line: int,
    *,
    depth: int = DEFAULT_DEPTH,
) -> InterprocSlice | None:
    """Backward interprocedural slice from ``seed_path:seed_line``. ``None`` if the seed line is not
    inside a parseable function (Python or JS/TS). ``seed_path`` must match the store's repo-rel
    paths; callees are sliced in their own file's language."""
    lang = _lang_for(seed_path)
    source = read_source(seed_path)
    if lang is None or source is None:
        return None
    func = function_at(source, seed_line, lang)
    if func is None or func.child_by_field_name(lang.body_field) is None:
        return None
    pdg = build_pdg(func, lang)
    seed_node = pdg.seed_for_line(seed_line)
    if seed_node is None:
        return None
    seed_ids = pdg.backward_slice([seed_node])
    seed_lines = {pdg.cfg.nodes[n].start_line for n in seed_ids if n in pdg.cfg.nodes}

    seed_symbol = _symbol_at(store, seed_path, seed_line)
    seed_qn = seed_symbol.qualified_name if seed_symbol else f"{seed_path}:{seed_line}"
    functions: dict[str, FunctionSlice] = {
        seed_qn: FunctionSlice(seed_qn, seed_path, True, _statements(pdg, seed_ids))
    }

    if seed_symbol is not None:
        visited = {seed_symbol.id}
        frontier = _callees_in_slice(store, seed_symbol, seed_lines)
        hop = 1
        while frontier and hop <= depth:
            nxt: list[Node] = []
            for callee in frontier:
                if callee.id in visited:
                    continue
                visited.add(callee.id)
                sliced = _output_slice(read_source, callee)
                if sliced is None:
                    continue
                statements, sliced_lines = sliced
                if callee.qualified_name not in functions:
                    functions[callee.qualified_name] = FunctionSlice(
                        callee.qualified_name, callee.location.path, False, statements
                    )
                nxt.extend(_callees_in_slice(store, callee, sliced_lines))
            frontier = nxt
            hop += 1

    ordered = tuple(
        sorted(functions.values(), key=lambda f: (not f.is_seed, f.path, f.qualified_name))
    )
    return InterprocSlice(f"{seed_path}:{seed_line}", depth, ordered)
