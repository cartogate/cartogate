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
from pathlib import Path
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
from cartogate.schema.enums import Confidence, EdgeType, Language, NodeKind, Visibility
from cartogate.schema.nodes import Node
from cartogate.store.base import Direction, StoreInterface

#: Cap on the candidate/hint names returned by a failed symbol lookup — enough to disambiguate,
#: small enough to never flood the agent's context.
_CANDIDATE_LIMIT = 8

#: Cap on a symbol-name query. No real qualified name approaches this; anything longer is junk
#: input not worth a full-graph scan.
_MAX_NAME_LEN = 512

#: Cap on source code lines returned by read_symbol. Prevents flooding the agent's context
#: with huge symbol bodies (templates, generated code, etc.).
MAX_SOURCE_LINES = 120

#: Cap on the top-level public exports listed per unit in repo_map's overview mode — enough to
#: convey a module's surface without dumping every symbol (the count over the cap is reported).
EXPORTS_CAP = 8

#: The synthetic unit holding external (third-party) package nodes. Excluded from repo_map's
#: overview, which describes the repo's own modules (mirrors families.py's local declaration).
_EXTERNALS_UNIT = "<externals>"

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
            "Before modifying an exported symbol, use this instead of grep to find what depends on "
            "it — returns RESOLVED dependents over structural edges, not raw text matches."
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
            "Use instead of grep when you need to resolve a symbol by name (bare, suffix, or "
            "fully qualified) — returns RESOLVED qnames with locations, not raw text. Grep is "
            "better for raw text/comments/config."
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
            "Use instead of grep to find all callers/references of a symbol — returns RESOLVED "
            "callers over structural edges with file:line, not raw text hits including comments "
            "and strings."
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
            "Use to detect module-level import cycles (circular dependencies) — a whole-program "
            "structural check no single-file view can make. Takes no input."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_duplicate_bodies",
        "description": (
            "Use to find groups of functions with identical copy-pasted bodies (even across "
            "renames) — catches what the signature-exact duplicate gate misses. Advisory, "
            "never blocks."
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
            "Use at PR time to summarize impact: for changed symbols, lists who is affected, which "
            "tests to run, and which docs may be stale. Pass `symbols` (bare/suffix/full names) or "
            "a unified `diff`. Advisory, never blocks."
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
            "Use when debugging a failing test: ranks likely culprits as symbols the test "
            "exercises that the change touched (reach ∩ diff), nearest to the test first. Pass "
            "the failing `test` (bare/suffix/full name) and a unified `diff`. The reach is "
            "searched up to `depth` hops (default 4). Advisory, never blocks."
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
            "Use to compute program slices (Python/JS/TS): given a `source` and 1-based `line`, "
            "returns statements that AFFECT that line (backward slice — control + data dependence) "
            "or statements that line AFFECTS (forward slice). For debugging, pass the source file "
            "containing the failing assertion. Advisory, never blocks."
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
            "Use to find top-level INTERNAL symbols with no in-repo reference — dead-code "
            "candidates. Conservative (internal-only, top-level-only; tests/entrypoints/dunders "
            "excluded). Advisory, never blocks; dynamic dispatch/reflection/registration can "
            "still make one live, so review before deleting. Takes no input."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_symbol",
        "description": (
            "Use when you need to SEE a symbol's actual code. One call instead of grep + reading "
            "the whole file — returns the exact source span. Shows location context and detects "
            "when root is unknown or file is missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"qualified_name": {"type": "string"}},
            "required": ["qualified_name"],
        },
    },
    {
        "name": "implementations",
        "description": (
            "Use when you need who implements/subclasses X. Grep text-matching misses resolved "
            "cross-file inheritance — this traverses the graph's INHERITS/IMPLEMENTS edges "
            "instead. Advisory, never blocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"qualified_name": {"type": "string"}},
            "required": ["qualified_name"],
        },
    },
    {
        "name": "repo_map",
        "description": (
            "First call in an unfamiliar repo: one call returns the module map — units, sizes, "
            "public exports, dependency direction — replacing the exploratory grep/ls storm. "
            "Use module=<unit path> to zoom into one file's exports and dependents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {
                    "type": "string",
                    "description": "Optional unit path to zoom into.",
                }
            },
            "required": [],
        },
    },
]


class CartogateTools:
    """Implements the MCP tools over a store (check_duplicate + the advisory queries)."""

    def __init__(self, store: StoreInterface, root: Path | str | None = None) -> None:
        self._store = store
        self._block = BlockEngine(store)
        self._traversal = GatingTraversal(store)
        self._flag = FlagEngine(store)
        self._root = Path(root) if root is not None else None

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

        # Attach exact call sites per caller. Iterate the caller NODES directly (not a name
        # re-match) so overloaded callers that share a qualified_name each get their own sites.
        # Constrain the site query to EXTRACTED, matching the gate traversal that produced the
        # callers — an INFERRED edge must never leak a site into this result (risk R7).
        refs = []
        for caller in sorted(callers, key=lambda n: n.qualified_name):
            edges = self._store.neighbors(
                caller.id,
                edge_types=REFERENCE_EDGE_TYPES,
                direction=Direction.OUT,
                confidence=(Confidence.EXTRACTED,),
            )
            sites = sorted({
                f"{e.source_location.path}:{e.source_location.line}"
                for e in edges
                if e.dst == node.id and e.source_location is not None
            })
            brief = _node_brief(caller)
            brief["sites"] = sites
            refs.append(brief)

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

    def implementations(self, qualified_name: str) -> dict[str, Any]:
        """Find all classes/interfaces that implement or subclass a given type.

        Resolves the base type by name (bare/suffix/full), then returns all nodes with
        INHERITS or IMPLEMENTS edges pointing to it. Sorted by qualified name for determinism.
        """
        node, candidates = self._resolve_symbol(qualified_name)
        if node is None:
            return {"found": False, "qualified_name": qualified_name, "candidates": candidates,
                    "implementations": [], "count": 0}
        # Query for both INHERITS and IMPLEMENTS edges (any subclass/implementer of node)
        inheritance_types = [EdgeType.INHERITS, EdgeType.IMPLEMENTS]
        impls = self._traversal.callers(node.id, depth=1, edge_types=inheritance_types)
        briefs = sorted((_node_brief(n) for n in impls), key=lambda b: b["qualified_name"])
        return {
            "found": True,
            "qualified_name": node.qualified_name,  # canonical
            "implementations": briefs,
            "count": len(briefs),
        }

    def read_symbol(self, qualified_name: str) -> dict[str, Any]:
        """Return the actual source code of a symbol.

        Resolves the symbol by name, then reads its source span from disk (if root is known),
        capping at MAX_SOURCE_LINES to prevent flooding the context.
        """
        node, candidates = self._resolve_symbol(qualified_name)
        if node is None:
            return {"found": False, "qualified_name": qualified_name, "candidates": candidates}

        result: dict[str, Any] = {
            "found": True,
            "qualified_name": node.qualified_name,
            "signature": node.signature,
            "location": f"{node.location.path}:{node.location.start_line}-{node.location.end_line}",
        }

        # If no root, return found=True but source=None with a note
        if self._root is None:
            result["source"] = None
            result["note"] = "workspace root unknown — pass root or use read tool"
            return result

        # Resolve the file. ``location.path`` is recorded relative to the index base
        # (root.parent — units are repo-prefixed, e.g. "myrepo/src/foo.py"), so it must be
        # resolved against the repo root's PARENT, not the repo root itself, or the repo
        # segment doubles and every read misses (the viz source_root=root.parent lesson).
        base = self._root.parent.resolve()
        file_path = (base / node.location.path).resolve()
        # Containment guard (defense-in-depth): a Path join silently discards the base when the
        # right side is absolute, so a non-conforming/absolute location.path would otherwise read
        # an arbitrary local file. location.path is producer-guaranteed relative today; this keeps
        # the read confined to the workspace regardless.
        if not file_path.is_relative_to(base):
            result["source"] = None
            result["note"] = f"source path outside workspace: {node.location.path}"
            result["truncated"] = False
            return result
        try:
            # UTF-8 to match the extract pipeline's own read (pipeline.py) — the platform default
            # (cp1252 on Windows) would silently mojibake any non-ASCII source.
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            result["source"] = None
            result["note"] = f"source file not found: {node.location.path}"
            result["truncated"] = False
            return result

        # Extract the source span (1-based line numbers, inclusive). split on "\n" to mirror
        # tree-sitter's row counting exactly; read_text() already normalizes CRLF/CR to "\n" via
        # universal-newline mode, so no carriage returns survive.
        lines = content.split("\n")
        start_idx = max(0, node.location.start_line - 1)
        end_idx = min(len(lines), node.location.end_line)
        source_lines = lines[start_idx:end_idx]

        # Cap at MAX_SOURCE_LINES
        truncated = len(source_lines) > MAX_SOURCE_LINES
        if truncated:
            source_lines = source_lines[:MAX_SOURCE_LINES]

        result["source"] = "\n".join(source_lines)
        result["truncated"] = truncated
        return result

    def repo_map(self, module: str | None = None) -> dict[str, Any]:
        """Repo orientation: list all units with exports, or zoom into one module.

        Overview mode (module=None): lists all units with symbol counts, top-level public exports
        (capped at EXPORTS_CAP=8), and summary statistics.

        Detail mode (module=str): for an exact unit match, returns full exports (uncapped) and
        the units that import from this one (dependents).

        Miss mode: returns found=False with candidates matching basename or path suffix.
        """
        if module is None:
            # Overview: iterate the repo's own units, collect exports + counts.
            units = []
            total_nodes = 0
            total_edges = 0

            for unit, nodes, edges in self._store.iter_unit_facts():
                # Skip the synthetic <externals> unit — it is not a file in the repo, and listing
                # it as one confuses the orientation this tool exists to give.
                if unit == _EXTERNALS_UNIT:
                    continue
                total_edges += len(edges)

                # Count SYMBOL nodes only — the per-file MODULE node is structural, not a symbol,
                # and counting it would report a file with 3 functions as having 4 symbols.
                unit_symbols = [n for n in nodes if n.kind is NodeKind.SYMBOL]
                total_nodes += len(unit_symbols)

                # Exports: top-level public symbols only.
                exports = [
                    {"name": n.name, "signature": n.signature}
                    for n in unit_symbols
                    if n.is_top_level and n.visibility is not Visibility.INTERNAL
                ]
                exports = sorted(exports, key=lambda e: str(e["name"]))

                # Cap exports
                capped_exports = exports[:EXPORTS_CAP]
                unit_dict: dict[str, Any] = {
                    "unit": unit,
                    "symbols": len(unit_symbols),
                    "exports": capped_exports,
                }
                if len(exports) > EXPORTS_CAP:
                    unit_dict["more"] = len(exports) - EXPORTS_CAP

                units.append(unit_dict)

            # Sort for cross-backend determinism — every other list result in this module does.
            units = sorted(units, key=lambda u: u["unit"])
            return {
                "units": units,
                "unit_count": len(units),
                "node_count": total_nodes,
                "edge_count": total_edges,
            }

        else:
            # Detail: exact unit match - search through all units
            target_nodes = None

            for unit, nodes, _edges in self._store.iter_unit_facts():
                if unit == module:
                    target_nodes = nodes
                    break

            if target_nodes is None:
                # No match: find candidates by basename or suffix
                all_units = list(self._store.units())
                basename = module.rsplit("/", 1)[-1]

                candidates = [
                    u for u in all_units
                    if u.endswith(basename) or u.endswith("/" + basename)
                ]
                # Also include units sharing the basename
                candidates += [u for u in all_units if u.rsplit("/", 1)[-1] == basename]
                candidates = sorted(set(candidates))[:5]

                return {
                    "found": False,
                    "unit": module,
                    "candidates": candidates,
                }

            # Exact match: get exports and dependents
            nodes = target_nodes

            # Exports: all top-level symbols (uncapped)
            exports = [
                {**_node_brief(n), "name": n.name}
                for n in nodes
                if n.kind is NodeKind.SYMBOL and n.is_top_level
            ]
            exports = sorted(exports, key=lambda e: str(e["qualified_name"]))

            # Dependents: units that import from this unit's nodes
            # Iterate through all units to find imports-type edges pointing to our nodes
            node_ids = {n.id for n in nodes}
            dependents_set = set()
            for other_unit, _, other_edges in self._store.iter_unit_facts():
                for edge in other_edges:
                    if edge.type is EdgeType.IMPORTS and edge.dst in node_ids:
                        # This unit imports from the target unit
                        dependents_set.add(other_unit)
                        break  # Found at least one import, can move to next unit

            dependents = sorted(dependents_set)

            return {
                "found": True,
                "unit": module,
                "exports": exports,
                "dependents": dependents,
            }


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
    if name == "read_symbol":
        return tools.read_symbol(arguments["qualified_name"])
    if name == "implementations":
        return tools.implementations(arguments["qualified_name"])
    if name == "repo_map":
        return tools.repo_map(module=arguments.get("module"))
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
    """A compact node view (qualified name, kind, unit, location, signature) for list results."""
    return {
        "qualified_name": node.qualified_name,
        "kind": node.kind.value,
        "unit": node.unit,
        "location": f"{node.location.path}:{node.location.start_line}",
        "signature": node.signature,
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
