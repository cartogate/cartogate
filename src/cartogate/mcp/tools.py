"""The deterministic MCP tools (spec §7.1) — model-free graph queries.

These are the agent-facing surface of the engine. They take plain JSON arguments and return
plain JSON results, with no dependency on the MCP transport, so they are unit-testable in
isolation and the server module is a thin adapter. All are deterministic and rest only on
EXTRACTED structural facts. ``check_duplicate`` is the hard gate; ``blast_radius`` /
``find_references`` / ``find_symbol`` / ``suggest_tests`` are advisory (FLAG mode reports, it
never blocks).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cartogate.engine.architecture import CYCLE_LIMIT
from cartogate.engine.architecture import find_cycles as _find_cycles
from cartogate.engine.block import BlockEngine
from cartogate.engine.clones import DEFAULT_MIN_LINES
from cartogate.engine.clones import find_duplicate_bodies as _find_duplicate_bodies
from cartogate.engine.deadcode import find_unreferenced_internal as _find_unreferenced_internal
from cartogate.engine.diff import parse_unified_diff as _parse_unified_diff
from cartogate.engine.flag import FlagEngine
from cartogate.engine.impact import build_impact_summary as _build_impact_summary
from cartogate.engine.impact import changed_symbol_qnames as _changed_symbol_qnames
from cartogate.engine.langspec import function_at, lang_for_name
from cartogate.engine.localize import DEFAULT_MAX_DEPTH
from cartogate.engine.localize import localize as _localize
from cartogate.engine.pdg import build_pdg as _build_pdg
from cartogate.engine.traversal import REFERENCE_EDGE_TYPES, GatingTraversal
from cartogate.schema.enums import EdgeType, Language, NodeKind
from cartogate.schema.nodes import Node
from cartogate.store.base import StoreInterface

#: Cap on the candidate/hint names returned by a failed symbol lookup — enough to disambiguate,
#: small enough to never flood the agent's context.
_CANDIDATE_LIMIT = 8

#: Cap on a symbol-name query. No real qualified name approaches this; anything longer is junk
#: input not worth a full-graph scan.
_MAX_NAME_LEN = 512

#: JSON-Schema tool definitions (name/description/input schema) for ``list_tools``.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "check_duplicate",
        "description": (
            "Before creating a function/class, check whether a symbol with the same "
            "signature already exists. Returns the existing symbol if so (reuse it)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "signature": {
                    "type": "string",
                    "description": "The signature being introduced, e.g. 'def login(user, pw):'.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "typescript", "java", "go", "rust", "javascript", "csharp",
                             "c", "cpp", "kotlin", "swift"],
                    "default": "python",
                    "description": "Source language (the gate is scoped per language).",
                },
            },
            "required": ["signature"],
        },
    },
    {
        "name": "blast_radius",
        "description": (
            "Before modifying an exported symbol, list the symbols that depend on it "
            "(its blast radius) over EXTRACTED structural edges."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol name — bare ('login'), dotted suffix "
                    "('auth.login'), or fully qualified.",
                },
                "depth": {"type": "integer", "minimum": 1, "default": 1},
                "edge_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional structural edge-type filter.",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "find_symbol",
        "description": (
            "Look up a symbol by name — bare ('login'), dotted suffix ('auth.login'), or "
            "fully qualified. On a miss the result lists candidate full names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"qualified_name": {"type": "string"}},
            "required": ["qualified_name"],
        },
    },
    {
        "name": "find_references",
        "description": (
            "List the symbols that reference or call a given symbol (bare/suffix/full name)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"qualified_name": {"type": "string"}},
            "required": ["qualified_name"],
        },
    },
    {
        "name": "suggest_tests",
        "description": (
            "After changing a function/class, list the tests that exercise it (so they can be "
            "reviewed/re-run). Advisory — never blocks. Pass `symbols` (bare/suffix/full "
            "names) or a unified `diff`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "diff": {"type": "string", "description": "A unified diff of the change."},
                "depth": {"type": "integer", "minimum": 1, "default": 1},
            },
        },
    },
    {
        "name": "doc_drift",
        "description": (
            "After changing a symbol, list the docs that explicitly reference it (and may now be "
            "stale). Advisory — never blocks. Pass `symbols` (bare/suffix/full names) or a "
            "unified `diff`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "diff": {"type": "string", "description": "A unified diff of the change."},
            },
        },
    },
    {
        "name": "find_cycles",
        "description": (
            "Architecture gate: list module-level import cycles (circular dependencies) in the "
            "graph — a whole-program check no single-file view can make. Takes no input."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_duplicate_bodies",
        "description": (
            "Advisory: list groups of functions whose body is an identical copy-paste (even "
            "across a rename) — what the signature-exact duplicate gate misses. Never blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "min_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Minimum symbol line-span (signature + body); symbols shorter than this "
                        "are skipped. Default 3."
                    ),
                }
            },
        },
    },
    {
        "name": "impact_summary",
        "description": (
            "PR-time impact summary: for the changed symbols, list who is affected (blast radius), "
            "which tests to run, and which docs may be stale — the three advisory views composed "
            "into one report. Pass `symbols` (bare/suffix/full names) or a unified `diff`. "
            "Never blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "diff": {"type": "string", "description": "A unified diff of the change."},
                "depth": {"type": "integer", "minimum": 1, "default": 1},
            },
        },
    },
    {
        "name": "localize",
        "description": (
            "Debug a failing test: rank the likely culprits as the symbols the test exercises that "
            "the change touched (reach ∩ diff), nearest to the test first. Pass the failing `test` "
            "(bare/suffix/full name) and a unified `diff` of the change. The reach is searched up "
            "to `depth` hops (default 4); raise it if the suspect may be deep. Advisory, never "
            "blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test": {
                    "type": "string",
                    "description": "Failing test's name — bare, dotted suffix, or fully qualified.",
                },
                "diff": {"type": "string", "description": "A unified diff of the change."},
                "depth": {"type": "integer", "minimum": 1, "default": DEFAULT_MAX_DEPTH},
            },
            "required": ["test"],
        },
    },
    {
        "name": "slice",
        "description": (
            "Program slice (Python or JS/TS): given a function's `source` and a 1-based `line`, "
            "return the statements that AFFECT that line (a backward slice — over control + data "
            "dependence), or with `forward: true` the statements that line AFFECTS. For debugging, "
            "pass the source of the file containing the failing assertion. Advisory — never blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "The source of the file."},
                "line": {"type": "integer", "description": "1-based line to slice from."},
                "forward": {"type": "boolean", "default": False},
                "language": {
                    "type": "string",
                    "enum": ["python", "typescript", "javascript", "go", "java", "c", "cpp"],
                    "default": "python",
                },
            },
            "required": ["source", "line"],
        },
    },
    {
        "name": "find_dead_code",
        "description": (
            "Advisory: list top-level INTERNAL symbols with no in-repo reference — dead-code "
            "candidates. Conservative (internal-only, top-level-only; tests/entrypoints/dunders "
            "excluded) and never blocks; dynamic dispatch / reflection / framework registration "
            "can still make one live (and in Go/Rust a function passed as a callback argument), so "
            "review before deleting. Takes no input."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


class CartogateTools:
    """Implements the MCP tools over a store (check_duplicate + the advisory queries)."""

    def __init__(self, store: StoreInterface) -> None:
        self._store = store
        self._block = BlockEngine(store)
        self._traversal = GatingTraversal(store)
        self._flag = FlagEngine(store)

    def _symbol_nodes(self) -> list[Node]:
        nodes = (self._store.get_node(i) for i in self._store.visible_node_ids())
        return [n for n in nodes if n is not None and n.kind is NodeKind.SYMBOL]

    def _resolve_symbol(self, name: str) -> tuple[Node | None, list[str]]:
        """Resolve a symbol name: exact qualified name, else a UNIQUE dotted-suffix match.

        Qualified names are rooted at the indexed directory's name (``sample_pkg.auth.login``) —
        a prefix agents don't know, so an exact-only lookup made every qname-keyed tool return
        correct-but-empty results. A bare or partially-qualified name (``login``, ``auth.login``)
        now resolves when unambiguous. Returns ``(node, candidates)``: on a miss, ``candidates``
        carries the matching full names — an ambiguous suffix's choices, symbols with the same last
        segment, or (when the name is a MODULE, e.g. ``find_symbol("query_kb")``) the symbols
        inside that module — sorted, so the answer is deterministic and the agent can qualify +
        retry. Malformed input (leading/trailing dots, absurd length) never matches the suffix
        pass; it degrades to the hint passes or an empty candidate list.
        """
        if len(name) > _MAX_NAME_LEN:  # defense-in-depth: don't scan the graph for junk input
            return None, []
        node = self._store.get_symbol(name)
        if node is not None:
            return node, []
        nodes = self._symbol_nodes()  # one scan serves the suffix and both hint passes
        suffix = "." + name
        matches = sorted(
            (n for n in nodes if n.qualified_name.endswith(suffix)),
            key=lambda n: n.qualified_name,
        )
        if len(matches) == 1:
            return matches[0], []
        if matches:  # ambiguous — never guess; report the choices
            return None, [n.qualified_name for n in matches[:_CANDIDATE_LIMIT]]
        last = name.rsplit(".", 1)[-1]
        hints = sorted(
            n.qualified_name for n in nodes if n.qualified_name.rsplit(".", 1)[-1] == last
        )
        if not hints:
            # The name may be a MODULE (agents ask for "query_kb" when they mean its functions):
            # offer the symbols living under that dotted segment.
            infix = "." + name + "."
            hints = sorted(n.qualified_name for n in nodes if infix in n.qualified_name)
        return None, hints[:_CANDIDATE_LIMIT]

    def _resolve_symbols(self, names: Iterable[str]) -> tuple[list[str], dict[str, list[str]]]:
        """Resolve a list of symbol names to canonical qnames + the unresolved ones (with hints)."""
        resolved: list[str] = []
        unresolved: dict[str, list[str]] = {}
        for name in names:
            node, candidates = self._resolve_symbol(name)
            if node is not None:
                resolved.append(node.qualified_name)
            else:
                unresolved[name] = candidates
        return resolved, unresolved

    def find_cycles(self) -> dict[str, Any]:
        """Architecture gate: module-level import cycles (circular dependencies) in the graph.

        A whole-program check — invisible to a single-file view. Each cycle is a list of module
        qnames; the result is deterministic.
        """
        cycles = _find_cycles(self._store)
        # `truncated` surfaces the rare pathological-graph cap rather than silently dropping cycles.
        return {"cycles": cycles, "count": len(cycles), "truncated": len(cycles) >= CYCLE_LIMIT}

    def find_duplicate_bodies(self, min_lines: int = DEFAULT_MIN_LINES) -> dict[str, Any]:
        """Advisory: groups of functions whose body is an identical copy-paste (even across a
        rename). High-confidence (identical normalized body); never blocks. Each group is a list
        of qualified names. ``min_lines`` is the size floor that keeps trivial one-liners out.
        """
        groups = _find_duplicate_bodies(self._store, min_lines=min_lines)
        return {"groups": groups, "count": len(groups)}

    def find_dead_code(self) -> dict[str, Any]:
        """Advisory: top-level INTERNAL symbols with no in-repo reference — dead-code candidates.
        Conservative (internal-only, top-level-only, EXTRACTED; tests/entrypoints/dunders excluded)
        and never blocks; dynamic dispatch / reflection / registration can still make one live, so
        review before deleting. Each item is ``{qualified_name, location}``.
        """
        candidates = _find_unreferenced_internal(self._store)
        return {
            "candidates": [
                {"qualified_name": dead.qualified_name, "location": dead.location}
                for dead in candidates
            ],
            "count": len(candidates),
        }

    def check_duplicate(
        self,
        signature: str,
        language: str = "python",
        exclude_unit: str | None = None,
        proposed_body_hash: str | None = None,
        proposed_is_type_decl: bool | None = None,
    ) -> dict[str, Any]:
        # exclude_unit == surfaces.gate_proposed_source's editing_unit / the hook's _editing_unit
        # (the file being edited; same concept threaded down from the PreToolUse path).
        result = self._block.check_duplicate(
            signature,
            Language(language),
            exclude_unit=exclude_unit,
            proposed_body_hash=proposed_body_hash,
            proposed_is_type_decl=proposed_is_type_decl,
        )
        return {
            "blocked": result.blocked,
            "kind": result.kind.value,
            "reason": result.reason,
            "existing_symbol_id": result.existing_symbol_id,
            "existing_qualified_name": result.existing_qualified_name,
            # Actionable: where to find/reuse the existing symbol (F-66). existing_signature is the
            # raw source spelling (the `reason` carries the canonical normalized form).
            "existing_location": result.existing_location,
            "existing_signature": result.existing_signature,
            # The agent-facing block message (STRATEGY.md law 1): BLOCKED -> EVIDENCE -> the ONE
            # sanctioned ACTION -> anti-loop. Empty when not blocked.
            "action": result.action(),
            "message": result.agent_message(),
            # A signature-only TYPE-DECLARATION match is not blocked (name+bases is idiomatic;
            # copy-paste is verified by body hash at the write/commit gates) — but the caller
            # still gets the near match to inspect instead of a silent all-clear.
            "near_match": (
                {
                    "qualified_name": result.existing_qualified_name,
                    "location": result.existing_location,
                    "signature": result.existing_signature,
                }
                if not result.blocked and result.existing_qualified_name is not None
                else None
            ),
        }

    def blast_radius(
        self, symbol: str, depth: int = 1, edge_types: Iterable[str] | None = None
    ) -> dict[str, Any]:
        node, candidates = self._resolve_symbol(symbol)
        if node is None:
            return {"found": False, "symbol": symbol, "candidates": candidates,
                    "affected": [], "count": 0}
        types = _parse_edge_types(edge_types)
        affected = self._traversal.callers(node.id, depth=depth, edge_types=types)
        # Sorted by qualified name for a stable, cross-process-deterministic contract.
        briefs = sorted((_node_brief(n) for n in affected), key=lambda b: b["qualified_name"])
        return {
            "found": True,
            "symbol": node.qualified_name,  # canonical — teaches the agent the full name
            "depth": depth,
            "affected": briefs,
            "count": len(briefs),
        }

    def find_symbol(self, qualified_name: str) -> dict[str, Any]:
        node, candidates = self._resolve_symbol(qualified_name)
        if node is None:
            return {"found": False, "qualified_name": qualified_name, "candidates": candidates}
        return {"found": True, **_node_full(node)}

    def find_references(self, qualified_name: str) -> dict[str, Any]:
        node, candidates = self._resolve_symbol(qualified_name)
        if node is None:
            return {"found": False, "qualified_name": qualified_name, "candidates": candidates,
                    "references": [], "count": 0}
        callers = self._traversal.callers(node.id, depth=1, edge_types=REFERENCE_EDGE_TYPES)
        refs = sorted((_node_brief(n) for n in callers), key=lambda b: b["qualified_name"])
        return {
            "found": True,
            "qualified_name": node.qualified_name,  # canonical
            "references": refs,
            "count": len(refs),
        }

    def suggest_tests(
        self,
        symbols: Iterable[str] | None = None,
        diff: str | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        """FLAG: list the tests exercising the changed symbols (advisory; ``diff`` wins).

        ``symbols`` are qualified names (the FlagEngine's ``qualified_names``).
        """
        if diff:
            return self._flag.tests_for_diff(diff, depth=depth).to_dict()
        resolved, unresolved = self._resolve_symbols(symbols or [])
        report = self._flag.tests_for_symbols(resolved, depth=depth).to_dict()
        if unresolved:
            report["unresolved_symbols"] = unresolved
        return report

    def doc_drift(
        self, symbols: Iterable[str] | None = None, diff: str | None = None
    ) -> dict[str, Any]:
        """FLAG: list the docs that explicitly reference the changed symbols (advisory)."""
        if diff:
            return self._flag.docs_for_diff(diff).to_dict()
        resolved, unresolved = self._resolve_symbols(symbols or [])
        report = self._flag.docs_for_symbols(resolved).to_dict()
        if unresolved:
            report["unresolved_symbols"] = unresolved
        return report

    def impact_summary(
        self,
        symbols: Iterable[str] | None = None,
        diff: str | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        """Advisory PR-time summary for the changed symbols: who is affected (blast radius) + which
        tests to run + which docs may be stale, composed into one report. ``diff`` (a unified diff)
        wins over ``symbols`` (qualified names). Never blocks.
        """
        if diff:
            qnames = _changed_symbol_qnames(self._store, _parse_unified_diff(diff))
            return _build_impact_summary(self._store, qnames, depth=depth).to_dict()
        resolved, unresolved = self._resolve_symbols(symbols or [])
        report = _build_impact_summary(self._store, resolved, depth=depth).to_dict()
        if unresolved:
            report["unresolved_symbols"] = unresolved
        return report

    def localize(
        self, test: str, diff: str | None = None, depth: int = DEFAULT_MAX_DEPTH
    ) -> dict[str, Any]:
        """Advisory: rank likely culprits for a failing ``test`` — the symbols it exercises that the
        ``diff`` touched, nearest first. Without a ``diff`` there is no change signal (empty).
        """
        # The test name resolves like any symbol (bare/suffix accepted); an unresolvable one is
        # passed through untouched so the engine reports its own not-found shape.
        node, _candidates = self._resolve_symbol(test)
        test_qname = node.qualified_name if node is not None else test
        regions = _parse_unified_diff(diff) if diff else []
        return _localize(self._store, test_qname, regions, max_depth=depth).to_dict()

    def slice(
        self, source: str, line: int, forward: bool = False, language: str = "python"
    ) -> dict[str, Any]:
        """Advisory program slice (Python or JS/TS): statements that affect ``line`` (or, with
        ``forward``, that ``line`` affects) within its function. ``source`` is the file's text —
        slicing needs the AST, which the symbol store doesn't keep. ``found=False`` when the
        ``language`` is unsupported or no function/statement is at ``line``.
        """
        lang = lang_for_name(language)
        if lang is None:
            return {"found": False, "reason": f"unsupported language {language!r}"}
        func = function_at(source.encode("utf-8"), line, lang)
        if func is None:
            return {"found": False, "reason": f"line {line} is not inside a function"}
        pdg = _build_pdg(func, lang)
        seed = pdg.seed_for_line(line)
        if seed is None:
            return {"found": False, "reason": f"no statement at line {line}"}
        sliced = pdg.forward_slice([seed]) if forward else pdg.backward_slice([seed])
        return {"found": True, "forward": forward, **pdg.to_dict(sliced)}


def dispatch(tools: CartogateTools, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call by name to the matching method. Raises on an unknown tool."""
    if name == "check_duplicate":
        # exclude_unit is hook-internal (the PreToolUse edit path) — intentionally not in
        # TOOL_SPECS, since a direct check_duplicate caller has no "editing this file" context.
        return tools.check_duplicate(
            arguments["signature"],
            language=arguments.get("language", "python"),
            exclude_unit=arguments.get("exclude_unit"),
            proposed_body_hash=arguments.get("proposed_body_hash"),
            proposed_is_type_decl=arguments.get("proposed_is_type_decl"),
        )
    if name == "blast_radius":
        return tools.blast_radius(
            arguments["symbol"],
            depth=int(arguments.get("depth", 1)),
            edge_types=arguments.get("edge_types"),
        )
    if name == "find_symbol":
        return tools.find_symbol(arguments["qualified_name"])
    if name == "find_references":
        return tools.find_references(arguments["qualified_name"])
    if name == "suggest_tests":
        return tools.suggest_tests(
            symbols=arguments.get("symbols"),
            diff=arguments.get("diff"),
            depth=int(arguments.get("depth", 1)),
        )
    if name == "doc_drift":
        return tools.doc_drift(symbols=arguments.get("symbols"), diff=arguments.get("diff"))
    if name == "impact_summary":
        return tools.impact_summary(
            symbols=arguments.get("symbols"),
            diff=arguments.get("diff"),
            depth=int(arguments.get("depth", 1)),
        )
    if name == "find_cycles":
        return tools.find_cycles()
    if name == "localize":
        return tools.localize(
            arguments["test"],
            diff=arguments.get("diff"),
            depth=int(arguments.get("depth", DEFAULT_MAX_DEPTH)),
        )
    if name == "slice":
        return tools.slice(
            arguments["source"],
            int(arguments["line"]),
            forward=bool(arguments.get("forward", False)),
            language=str(arguments.get("language", "python")),
        )
    if name == "find_dead_code":
        return tools.find_dead_code()
    if name == "find_duplicate_bodies":
        return tools.find_duplicate_bodies(
            min_lines=int(arguments.get("min_lines", DEFAULT_MIN_LINES))
        )
    raise ValueError(f"unknown tool: {name!r}")


def _parse_edge_types(edge_types: Iterable[str] | None) -> list[EdgeType] | None:
    """Parse edge-type name strings into EdgeType, dropping unknown names."""
    if edge_types is None:
        return None
    # Unknown names are ignored rather than fatal; the gating traversal intersects with the
    # allowed set anyway, so a typo simply contributes nothing.
    parsed: list[EdgeType] = []
    for raw in edge_types:
        try:
            parsed.append(EdgeType(raw))
        except ValueError:
            continue
    return parsed


def _node_brief(node: Node) -> dict[str, Any]:
    """A compact node view (qualified name, kind, unit) for list results."""
    return {
        "qualified_name": node.qualified_name,
        "kind": node.kind.value,
        "unit": node.unit,
    }


def _node_full(node: Node) -> dict[str, Any]:
    """A full node view (id, signature, visibility, location) for find_symbol."""
    return {
        "id": node.id,
        "qualified_name": node.qualified_name,
        "kind": node.kind.value,
        "name": node.name,
        "unit": node.unit,
        "signature": node.signature,
        "visibility": node.visibility.value,
        "location": {
            "path": node.location.path,
            "start_line": node.location.start_line,
            "end_line": node.location.end_line,
        },
    }
