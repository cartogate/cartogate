"""Extraction pipeline: source tree → CPG-shaped nodes/edges → store (spec §5.1, §13).

Two passes over the package:

1. **Structural pass (tree-sitter).** Walk every file into symbol/module nodes and the
   positions of the names that need binding. Build the cross-file lookup indices.
2. **Resolution pass (jedi).** Bind each name occurrence to a concrete node and emit the
   typed edge. Calls/references/imports/inherits that resolve outside the repo become
   ``external_package`` nodes (imports/inherits) or are skipped (calls/references).

Every emitted node and edge is tagged ``confidence=EXTRACTED``; structural facts carry
``provenance=tree-sitter`` and resolved facts carry ``provenance=lsp`` (jedi ≈ LSP). Facts
are written to the store one unit (file) at a time, plus a synthetic ``<externals>`` unit
that owns the shared external-package nodes (so they never collide across file units).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Collection, Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    FileFacts,
    RawName,
    RawSymbol,
)
from cartogate.extract.languages import (
    LANGUAGES,
    SOURCE_SUFFIXES,
    SUFFIX_TO_LANGUAGE,
    language_of,
)
from cartogate.extract.languages import module_qname as _lang_module_qname
from cartogate.extract.resolver import NameResolver, Resolved
from cartogate.gitio import run_git
from cartogate.instrument import NULL_SPAN_HANDLE, Phase, SpanRecorder
from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import (
    Confidence,
    EdgeType,
    Language,
    NodeKind,
    Provenance,
    Visibility,
)
from cartogate.schema.nodes import Location, Node
from cartogate.schema.signature import normalize_signature
from cartogate.store.base import StoreInterface

#: Unit that owns the shared external-package nodes.
EXTERNALS_UNIT = "<externals>"

#: Skip pathologically large files (generated/vendored blobs) — they are not worth
#: parsing on the index path and guard against runaway memory.
MAX_FILE_BYTES = 2_000_000

#: Directory names never worth indexing — virtualenvs, vendored deps, caches, VCS. The
#: *fallback* (non-git) exclusion set; inside a git repo, ``git_tracked_files`` (F-38) supersedes
#: this by respecting the real ``.gitignore``. Kept for non-git trees.
_EXCLUDED_DIRS = frozenset(
    {
        ".venv", "venv", "env", "site-packages", "__pycache__", ".git", "node_modules",
        "build", "dist", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
    }
)


_LOG = logging.getLogger("cartogate")

#: Soft ceilings beyond which the in-memory NetworkX backend's whole-graph (re)build and resident
#: footprint start to bite (spec §8.6 trip-wire). Crossing one is a *signal* to move to a
#: persistent/indexed backend, not an error — indexing still completes and the gate still works.
SCALE_TRIPWIRE_NODES = 200_000
SCALE_TRIPWIRE_SECONDS = 30.0


def scale_warning(node_count: int, edge_count: int, seconds: float) -> str | None:
    """Return a one-line scale warning if an index crosses the §8.6 ceilings, else ``None``.

    Pure (no I/O) so the threshold logic is unit-testable; the caller logs the message.
    """
    reasons: list[str] = []
    if node_count >= SCALE_TRIPWIRE_NODES:
        reasons.append(f"{node_count:,} nodes ≥ {SCALE_TRIPWIRE_NODES:,}")
    if seconds >= SCALE_TRIPWIRE_SECONDS:
        reasons.append(f"indexed in {seconds:.1f}s ≥ {SCALE_TRIPWIRE_SECONDS:.0f}s")
    if not reasons:
        return None
    return (
        "Cartogate index is large (" + "; ".join(reasons) + f", {edge_count:,} edges). The "
        "in-memory backend rebuilds the whole graph on each (re)index and holds it in RAM; for a "
        "repo this size consider a persistent/indexed backend (spec §8.6)."
    )


def git_tracked_files(root: Path) -> list[Path] | None:
    """The git working set under ``root`` — tracked + untracked-but-not-ignored files — respecting
    every ``.gitignore`` (and ``.git/info/exclude`` / the global excludes). Returns absolute paths,
    or ``None`` if ``root`` is not in a git repo or ``git`` is unavailable (caller falls back to a
    fixed-excluded-dir walk). The complete fix for indexing vendored/generated trees (F-38)."""
    # run_git hardens against the Windows pipe-inheritance hang (a git child holding the captured
    # pipe open -> communicate() blocks forever) and bounds it with a real timeout; None on any
    # not-a-repo / git-missing / hung case -> caller uses the fixed-dir fallback.
    out = run_git(
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
        cwd=root,
        timeout=10,
    )
    if out is None:
        return None
    rels = out.decode("utf-8", "replace").split("\0")
    # ls-files paths are repo-relative to ``root``; resolve to real (correct-case) absolute paths.
    return [(root / rel) for rel in rels if rel]


def _indexable(path: Path) -> bool:
    """A real, not-too-large file (symlinks and oversized blobs are skipped on the index path)."""
    if path.is_symlink():
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def iter_files(root: Path, suffix: str, allow: list[Path] | None = None) -> Iterator[Path]:
    """Yield indexable files with ``suffix`` under ``root`` (skip symlinks, blobs, noise dirs).

    When ``allow`` (the git working set from :func:`git_tracked_files`) is given, yield only those
    files — never walking ignored/vendored trees at all. Otherwise fall back to a pruned manual
    walk that never enters the fixed noise-dir set (for non-git trees). NOTE: unlike the old
    ``rglob``, the fallback walk does NOT follow symlinked directories (cycle/vanished-target
    safety) — a repo that symlinks source trees into its root indexes fewer files than before;
    use git (the ``allow`` path) there."""
    if allow is not None:
        for path in allow:
            # The git layer already drops gitignored trees; `_EXCLUDED_DIRS` stays as a belt-and-
            # suspenders guard for the "untracked .venv that wasn't gitignored" case.
            if (
                path.suffix == suffix
                and not _EXCLUDED_DIRS.intersection(path.parts)
                and _indexable(path)
            ):
                yield path
        return
    # Manual pruned walk, NOT rglob: rglob physically descends into excluded trees before we can
    # filter its results — a pnpm node_modules store nests paths past Windows' limit and holds
    # dangling junctions, so the traversal itself raised FileNotFoundError and killed the whole
    # index. Pruning skips those trees entirely, and any unreadable/vanished/too-long directory is
    # skipped instead of fatal.
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _EXCLUDED_DIRS:
                                stack.append(Path(entry.path))
                        elif entry.name.endswith(suffix) and not entry.is_symlink():
                            found.append(Path(entry.path))
                    except OSError:
                        continue  # a single unstatable entry never kills the index
        except OSError:
            continue  # vanished/unreadable/path-too-long directory — skip it, keep indexing
    for path in sorted(found):
        if _indexable(path):
            yield path


def iter_source_files(root: Path) -> Iterator[tuple[Path, Language]]:
    """Yield every indexable source file under ``root`` tagged with its language. Respects
    ``.gitignore`` inside a git repo (computed once), else skips the fixed noise-dir set."""
    allow = git_tracked_files(root)
    for suffix in SOURCE_SUFFIXES:
        for path in iter_files(root, suffix, allow):
            yield path, SUFFIX_TO_LANGUAGE[suffix]

_RELATION_TO_EDGE = {
    NAME_CALL: EdgeType.CALLS,
    "reference": EdgeType.REFERENCES,
    NAME_IMPORT: EdgeType.IMPORTS,
    NAME_INHERIT: EdgeType.INHERITS,
}
#: jedi ``type`` values that name a real symbol definition we can link an edge to.
#: Everything else (param, statement, instance, keyword, ...) is intentionally excluded
#: so a use of a local/parameter never becomes a spurious edge.
_LINKABLE_SYMBOL_TYPES = {"function", "class"}


def _norm(path: str) -> str:
    """Case-normalize a path for membership tests (Windows drive/case insensitivity)."""
    return os.path.normcase(path)


@dataclass(slots=True)
class IndexResult:
    """Everything a single index pass produced (also written into the store)."""

    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    units: tuple[str, ...] = ()
    files_indexed: int = 0
    external_calls_skipped: int = 0
    #: Files whose name resolution raised and were degraded to structural-only (nodes + ``defines``
    #: edges, plus any resolved edges emitted before the crash). A resolver crash on one file no
    #: longer aborts the whole index; resolution is advisory, so these never affect the gate (R7).
    resolution_failures: int = 0
    #: Files dropped entirely (not even structural facts) because they could not be read or
    #: structurally parsed. Also non-fatal — the rest of the tree still indexes.
    files_skipped: int = 0


@dataclass(slots=True)
class _FilePlan:
    facts: FileFacts
    rel_path: str
    abs_path: str
    module_node: Node
    language: Language
    #: Whether this file *owns* (emits) its module node. False for the 2nd+ file of a shared
    #: module — Java packages span several files, which all reference one package module node.
    owns_module: bool = True
    symbol_nodes: list[Node] = field(default_factory=list)


@dataclass(slots=True)
class ResolutionContext:
    """The whole-repo resolution maps, rebuilt from a store's visible nodes, so an incremental
    ``index_package(paths=..., context=...)`` resolves a changed file against the *entire* repo
    instead of just itself (F-36). Without this, a re-extracted file's calls into unchanged files
    fail the in-repo membership test and the edges are silently dropped.
    """

    symboldef_by_loc: dict[tuple[str, int], Node]  # (norm abspath, start_line) -> symbol node
    abspaths: set[str]  # in-repo file set (norm abspaths) for the local-vs-external test
    module_by_abspath: dict[str, Node]  # norm abspath -> module node
    external_nodes: dict[tuple[Language, str], Node]  # (language, pkg) -> external_package node


def build_resolution_context(
    store: StoreInterface, base: Path, *, exclude_rels: Collection[str] = ()
) -> ResolutionContext:
    """Rebuild the whole-repo resolution maps from ``store``'s visible nodes for an incremental
    re-extract. ``exclude_rels`` (the rel POSIX paths about to be re-extracted) are omitted so their
    *stale* symbols don't shadow the fresh ones Pass 1 will produce.

    Exact for unique-module languages (Python/TS). The abspath→module map for *shared* (Java/Go)
    packages is not fully recoverable from the store, so the daemon full-reindexes those changes.
    """
    base = base.resolve()
    exclude = set(exclude_rels)
    symboldef_by_loc: dict[tuple[str, int], Node] = {}
    abspaths: set[str] = set()
    module_by_abspath: dict[str, Node] = {}
    external_nodes: dict[tuple[Language, str], Node] = {}
    for unit, nodes, _edges in store.iter_unit_facts():
        if unit in exclude:
            continue
        for node in nodes:
            if node.kind is NodeKind.EXTERNAL_PACKAGE:
                external_nodes[(node.language, node.qualified_name)] = node
                continue
            # Same key _resolve_edge computes: _norm(str(def_path.resolve())). base/rel IS the file.
            abs_path = _norm(str((base / node.location.path).resolve()))
            if node.kind is NodeKind.MODULE:
                module_by_abspath[abs_path] = node
                abspaths.add(abs_path)
            elif node.kind is NodeKind.SYMBOL:
                symboldef_by_loc[(abs_path, node.location.start_line)] = node
                abspaths.add(abs_path)
    return ResolutionContext(symboldef_by_loc, abspaths, module_by_abspath, external_nodes)


def index_package(
    root: Path,
    *,
    repo_id: str,
    store: StoreInterface,
    recorder: SpanRecorder | None = None,
    base: Path | None = None,
    resolve: bool = True,
    index_docs: bool = True,
    paths: Sequence[Path] | None = None,
    context: ResolutionContext | None = None,
) -> IndexResult:
    """Index every supported source file under ``root`` into ``store`` and return the facts.

    ``paths`` restricts the index to a specific set of files (used by the daemon's incremental
    refresh to re-extract only what changed); ``None`` indexes the whole tree. The facts are
    ``bulk_load``-ed, so passing a subset *upserts* just those units and leaves the rest — but a
    subset is only sound when each file's module is unique to it (the caller's responsibility;
    files that share a module, e.g. a Java package, must be indexed together).

    Python and TypeScript files are dispatched to their language's structural walker, then to
    its name resolver (jedi for Python, a pure-Python resolver for TypeScript) to bind
    ``calls``/``references``/``imports``/``inherits`` edges. A language with no resolver gets
    nodes + ``defines`` only.

    **Resilient to one bad file.** A file that can't be read is skipped; a structural-walk crash
    skips that file (both counted in ``IndexResult.files_skipped``); a name-resolution crash
    degrades that file to structural-only (counted in ``resolution_failures``). None aborts the
    index, and none affects the gate (resolution is advisory; the signature table is structural).

    Args:
        root: Package directory to index.
        repo_id: Repository id stamped onto every node.
        store: Destination store (facts are upserted unit-by-unit).
        recorder: Optional span recorder; the resolution pass emits a ``resolution`` span.
        base: Import root for qualified names / relative paths (defaults to ``root.parent``).
        resolve: When ``False``, skip jedi name resolution — build only the symbol/module
            nodes and structural ``defines`` edges. The duplicate gate (``check_duplicate``)
            needs only the signature table, so this fast path lets the latency-sensitive
            surfaces (the PreToolUse hook) avoid paying the resolution cost (risk R19). No
            ``calls``/``references``/``imports``/``inherits`` edges or external nodes are
            produced in this mode.
        paths: Restrict indexing to these files (incremental re-extract); ``None`` indexes the
            whole tree.
        context: When set, seeds the four resolution maps from a prebuilt
            :class:`ResolutionContext` so a ``paths=`` re-extract resolves against the *whole* repo,
            not just itself (F-36). Build it with :func:`build_resolution_context` using the same
            ``base`` and excluding the files being re-extracted.
    """
    started = time.monotonic()
    base = (base or root.parent).resolve()
    if paths is None:
        files = list(iter_source_files(root))
    else:
        # Index only the named files (incremental refresh). Keep just the indexable ones.
        files = [(p.resolve(), lang) for p in paths if (lang := language_of(str(p))) is not None]

    files_skipped = 0
    resolution_failures = 0
    # Read sources resiliently: malformed bytes are replaced (never raise on decode), and a file we
    # genuinely can't read is skipped, not fatal — one bad file must not abort the whole index.
    sources: dict[str, str] = {}
    readable: list[tuple[Path, Language]] = []
    for p, lang in files:
        abspath = str(p.resolve())
        try:
            sources[abspath] = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            files_skipped += 1
            _LOG.warning("cartogate: could not read %s (%s); skipped", abspath, exc)
            continue
        readable.append((p, lang))
    files = readable

    walkers = {lang: LANGUAGES[lang].make_walker() for lang in {lang for _, lang in files}}
    plans: list[_FilePlan] = []
    qname_to_node: dict[str, Node] = {}
    # qname -> all its symbol nodes; usually one, but >1 for overloaded methods that share a qname.
    # Used to attribute a name occurrence to the *specific* enclosing overload (by line range).
    qname_to_nodes: dict[str, list[Node]] = {}
    # Seed the resolution maps from the whole-repo context (incremental re-extract): a changed file
    # then resolves against every unchanged file, not just itself. ``context`` excludes the files
    # being re-extracted, so Pass 1 below adds their fresh entries without colliding.
    module_by_abspath: dict[str, Node] = dict(context.module_by_abspath) if context else {}
    symboldef_by_loc: dict[tuple[str, int], Node] = (
        dict(context.symboldef_by_loc) if context else {}
    )

    # --- Pass 1: structural extraction + node construction (per language) ---
    for path, language in files:
        spec = LANGUAGES[language]
        abs_path = str(path.resolve())
        rel_path = path.resolve().relative_to(base).as_posix()
        module_qname = _lang_module_qname(rel_path, spec)
        source = sources[abs_path]
        # A structural-walk failure on one pathological file must not abort the index — skip just
        # this file (no plan, so it contributes nothing and is excluded from `abspaths` below, which
        # keeps resolution from emitting a dangling edge into it). Crash happens before any shared
        # state is mutated for this file, so nothing partial is left behind.
        try:
            facts = walkers[language].walk(
                source.encode("utf-8"),
                module_qname=module_qname,
                rel_path=rel_path,
                abs_path=abs_path,
            )
        except Exception as exc:
            files_skipped += 1
            _LOG.warning(
                "cartogate: structural extraction failed for %s (%s: %s); skipped",
                rel_path,
                type(exc).__name__,
                exc,
            )
            _LOG.debug("cartogate: structural extraction traceback", exc_info=True)
            continue

        # Several files can share one module namespace (Java: a package spans many files). The
        # first file to reach a module owns its node; later files reference the same node so the
        # store never sees a duplicate id across units. Python/TS module qnames are unique per
        # file, so this is always a fresh module for them (no behaviour change).
        existing_module = qname_to_node.get(module_qname)
        if existing_module is not None and existing_module.kind is NodeKind.MODULE:
            module_node = existing_module
            owns_module = False
        else:
            module_node = _make_module_node(repo_id, module_qname, rel_path, source, language)
            qname_to_node[module_qname] = module_node
            qname_to_nodes[module_qname] = [module_node]  # module-scope names source from here
            owns_module = True
        plan = _FilePlan(
            facts=facts,
            rel_path=rel_path,
            abs_path=abs_path,
            module_node=module_node,
            language=language,
            owns_module=owns_module,
        )
        module_by_abspath[_norm(abs_path)] = module_node

        # Distinct normalized signatures under one qname are distinct symbols (Java by-type method
        # overloads — ``add(int)`` vs ``add(String)``); an identical signature redeclared (a
        # TypeScript/Python ``@overload`` stub, an interface method + its impl) collapses to one.
        sigs_by_qname: dict[str, list[str]] = {}
        for sym in facts.symbols:
            sigs = sigs_by_qname.setdefault(sym.qualified_name, [])
            nsig = normalize_signature(sym.signature, language)
            if nsig not in sigs:
                sigs.append(nsig)

        seen_keys: set[tuple[str, str]] = set()
        for sym in facts.symbols:
            nsig = normalize_signature(sym.signature, language)
            key = (sym.qualified_name, nsig)
            if key in seen_keys:  # an exact-signature redeclaration — one node, keep the first
                continue
            seen_keys.add(key)
            # Overloads share a qname, so give each a per-signature ``stmt_ordinal`` to keep node
            # ids distinct; a non-overloaded symbol stays ``None`` (its id is unchanged).
            distinct = sigs_by_qname[sym.qualified_name]
            ordinal = sorted(distinct).index(nsig) if len(distinct) > 1 else None
            # Top-level iff its container is the module itself (a free function or top-level
            # class/interface); methods and nested functions have a class/function container.
            is_top_level = sym.container_qname == module_qname
            node = _make_symbol_node(
                repo_id, sym, rel_path, is_top_level=is_top_level, language=language,
                stmt_ordinal=ordinal,
            )
            plan.symbol_nodes.append(node)
            qname_to_node[sym.qualified_name] = node
            qname_to_nodes.setdefault(sym.qualified_name, []).append(node)
            symboldef_by_loc[(_norm(abs_path), sym.start_line)] = node
        plans.append(plan)

    # In-repo file set for the resolvers' local-vs-external test (all languages), case-normalized
    # so def-path membership is Windows-robust. Built from successfully-planned files, so a file
    # skipped in Pass 1 (a walk crash) is treated as external — never a dangling edge target.
    abspaths = {_norm(p.abs_path) for p in plans} | (context.abspaths if context else set())
    # One resolver per language present that has one (jedi for Python, the TS resolver for TS).
    # Resolution is the expensive part, so only stand resolvers up when ``resolve`` is set.
    resolvers: dict[Language, NameResolver] = {}
    if resolve:
        for language in {lang for _, lang in files}:
            make_resolver = LANGUAGES[language].make_resolver
            if make_resolver is None:
                continue
            lang_sources = {
                str(p.resolve()): sources[str(p.resolve())] for p, lang in files if lang is language
            }
            try:
                resolvers[language] = make_resolver(base, lang_sources)
            except Exception as exc:  # a resolver that won't build -> language is structural-only
                resolution_failures += len(lang_sources)
                _LOG.warning(
                    "cartogate: could not build the %s resolver (%s: %s); "
                    "those files degrade to structural-only",
                    language.value,
                    type(exc).__name__,
                    exc,
                )
                _LOG.debug("cartogate: resolver build traceback", exc_info=True)

    # --- Pass 2: resolution + edge construction (instrumented) ---
    # Seeded from the context so an incremental re-extract unions into the existing <externals>
    # rather than replacing it with only the changed file's externals (which would drop the rest).
    external_nodes: dict[tuple[Language, str], Node] = (
        dict(context.external_nodes) if context else {}
    )
    unit_edges: dict[str, set[Edge]] = {}
    external_calls_skipped = 0

    # Only emit a resolution span when resolution actually runs (not on the fast path).
    span_cm = (
        recorder.span(Phase.RESOLUTION, name="resolve")
        if recorder is not None and resolvers
        else nullcontext(NULL_SPAN_HANDLE)
    )
    with span_cm as handle:
        for plan in plans:
            edges = unit_edges.setdefault(plan.rel_path, set())
            # Structural ``defines`` edges (container -> symbol), no resolution needed. The
            # container is the symbol's qname minus its last segment (the enclosing module or
            # class) — derived from the node so it survives overload dedup of facts.symbols.
            for node in plan.symbol_nodes:
                container = qname_to_node.get(node.qualified_name.rsplit(".", 1)[0])
                if container is not None:
                    edges.add(
                        _defines_edge(container, node, plan.rel_path, node.location.start_line)
                    )
            # Resolved edges via the plan's language resolver. Fast mode and structural-only
            # languages have no resolver here -> they get nodes + ``defines`` only.
            resolver = resolvers.get(plan.language)
            if resolver is None:
                continue
            # Resolution is best-effort and advisory: a resolver crash on one pathological file
            # (e.g. a jedi internal error / corrupted cache) must NOT abort the whole index. Isolate
            # per file — on failure this file keeps its nodes + ``defines`` edges (emitted above)
            # and any resolved edges added before the crash (a valid partial result), then indexing
            # continues. The gate is resolution-free, so a missing resolved edge can never change a
            # BLOCK (R7); only advisory views (blast radius / references / localize) lose edges.
            try:
                for name in plan.facts.names:
                    # Attribute the occurrence to its enclosing symbol. When the enclosing qname is
                    # overloaded (several nodes share it), pick the overload whose body contains
                    # this line so a call inside one overload isn't sourced from another.
                    src = _enclosing_source(qname_to_nodes.get(name.enclosing_qname), name.line)
                    if src is None:
                        continue
                    edge, skipped_call = _resolve_edge(
                        name=name,
                        src=src,
                        abs_path=plan.abs_path,
                        rel_path=plan.rel_path,
                        resolver=resolver,
                        repo_id=repo_id,
                        language=plan.language,
                        abspaths=abspaths,
                        module_by_abspath=module_by_abspath,
                        symboldef_by_loc=symboldef_by_loc,
                        external_nodes=external_nodes,
                    )
                    external_calls_skipped += skipped_call
                    if edge is not None:
                        edges.add(edge)
            except Exception as exc:  # any resolver failure -> degrade this file, keep indexing
                resolution_failures += 1
                _LOG.warning(
                    "cartogate: name resolution failed for %s (%s: %s); "
                    "degraded to structural-only",
                    plan.rel_path,
                    type(exc).__name__,
                    exc,
                )
                _LOG.debug("cartogate: name resolution traceback", exc_info=True)
        handle.set_counts(
            node_count=sum(1 + len(p.symbol_nodes) for p in plans),
            edge_count=sum(len(e) for e in unit_edges.values()),
        )

    # --- Collect every unit's facts, then write them in ONE bulk load ---
    # Staging the units and rebuilding the store's derived graph once (rather than per
    # upsert_unit) keeps the initial index O(N) instead of O(units × N) — spec §8.6.
    pending: list[tuple[str, list[Node], list[Edge]]] = []
    all_nodes: list[Node] = list(external_nodes.values())
    all_edges: list[Edge] = []
    if external_nodes:
        pending.append((EXTERNALS_UNIT, list(external_nodes.values()), []))
    for plan in plans:
        # Only the owning file emits the (possibly shared) module node into its unit.
        file_nodes = ([plan.module_node] if plan.owns_module else []) + plan.symbol_nodes
        file_edges = list(unit_edges.get(plan.rel_path, set()))
        pending.append((plan.rel_path, file_nodes, file_edges))
        all_nodes.extend(file_nodes)
        all_edges.extend(file_edges)

    units = [EXTERNALS_UNIT] if external_nodes else []
    units += [p.rel_path for p in plans]

    # --- Doc pass: explicit markdown references -> doc_section nodes + documents edges ---
    # Advisory only (never gates). Skipped by the structural daemon (index_docs=False).
    if index_docs:
        from cartogate.extract.docs import extract_doc_facts  # local: avoid an import cycle

        symbols = [n for p in plans for n in p.symbol_nodes]
        modules = list({p.module_node.id: p.module_node for p in plans}.values())  # dedup shared
        doc_facts = extract_doc_facts(
            root, repo_id=repo_id, base=base, symbols=symbols, modules=modules,
            allow=git_tracked_files(root),
        )
        for doc_node in doc_facts.nodes:
            doc_edges = [e for e in doc_facts.edges if e.src == doc_node.id]
            pending.append((doc_node.unit, [doc_node], doc_edges))
            units.append(doc_node.unit)
        all_nodes.extend(doc_facts.nodes)
        all_edges.extend(doc_facts.edges)

    store.bulk_load(pending)

    warning = scale_warning(len(all_nodes), len(all_edges), time.monotonic() - started)
    if warning is not None:
        _LOG.warning(warning)

    return IndexResult(
        nodes=tuple(all_nodes),
        edges=tuple(all_edges),
        units=tuple(units),
        files_indexed=len(files),
        external_calls_skipped=external_calls_skipped,
        resolution_failures=resolution_failures,
        files_skipped=files_skipped,
    )


# --------------------------------------------------------------------------- #
# Edge construction
# --------------------------------------------------------------------------- #


def _resolve_edge(
    *,
    name: RawName,
    src: Node,
    abs_path: str,
    rel_path: str,
    resolver: NameResolver,
    repo_id: str,
    language: Language,
    abspaths: set[str],
    module_by_abspath: dict[str, Node],
    symboldef_by_loc: dict[tuple[str, int], Node],
    external_nodes: dict[tuple[Language, str], Node],
) -> tuple[Edge | None, int]:
    """Resolve one name occurrence to an edge. Returns (edge, external_call_skipped)."""
    edge_type = _RELATION_TO_EDGE[name.relation]
    resolved = resolver.resolve(abs_path, name.line, name.column)

    target: Node | None = None
    if resolved is not None and resolved.def_path is not None:
        def_abs = _norm(str(resolved.def_path.resolve()))
        if def_abs in abspaths:
            if resolved.def_type == "module":
                # A miss here despite def_abs ∈ abspaths means the maps came from a *partial*
                # ResolutionContext (a shared Java/Go module's non-owner abspath isn't recoverable
                # from the store). The daemon full-reindexes shared-module changes, so this can't
                # happen for the unique-module languages incremental refresh targets.
                target = module_by_abspath.get(def_abs)
            elif resolved.def_type in _LINKABLE_SYMBOL_TYPES and resolved.def_line is not None:
                target = symboldef_by_loc.get((def_abs, resolved.def_line))
            # An IMPORT of an in-repo module-level constant (or anything we don't model as
            # its own node) is attributed to its owning module, rather than leaking out as
            # a bogus external package. Only imports — a reference to a local variable must
            # not become an edge to its module.
            if target is None and name.relation == NAME_IMPORT:
                target = module_by_abspath.get(def_abs)

    if target is not None:
        return _edge(edge_type, src, target, rel_path, name.line), 0

    # Unresolved or external.
    if name.relation in (NAME_IMPORT, NAME_INHERIT):
        external = _external_node(repo_id, name, resolved, external_nodes, language)
        return _edge(edge_type, src, external, rel_path, name.line), 0
    # Calls/references to things outside the repo are not gateable edges; drop them.
    skipped = 1 if name.relation == NAME_CALL else 0
    return None, skipped


def _edge(edge_type: EdgeType, src: Node, dst: Node, rel_path: str, line: int) -> Edge:
    return Edge(
        type=edge_type,
        src=src.id,
        dst=dst.id,
        provenance=Provenance.LSP,
        confidence=Confidence.EXTRACTED,
        source_location=SourceLocation(path=rel_path, line=line),
    )


def _defines_edge(container: Node, symbol: Node, rel_path: str, line: int) -> Edge:
    return Edge(
        type=EdgeType.DEFINES,
        src=container.id,
        dst=symbol.id,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        source_location=SourceLocation(path=rel_path, line=line),
    )


def _external_node(
    repo_id: str,
    name: RawName,
    resolved: Resolved | None,
    external_nodes: dict[tuple[Language, str], Node],
    language: Language,
) -> Node:
    # Name the package, not the imported symbol: prefer the import's source module
    # (``collections.abc`` -> ``collections``), then jedi's resolved full name, then the
    # raw text. Leading dots (relative imports) are stripped before taking the top level.
    # Keyed by language so the same package name from two languages stays distinct.
    source = name.module or (resolved.full_name if resolved else "") or name.text
    top = source.lstrip(".").split(".")[0] or "<unknown>"
    key = (language, top)
    if key not in external_nodes:
        external_nodes[key] = Node.create(
            repo_id=repo_id,
            qualified_name=top,
            kind=NodeKind.EXTERNAL_PACKAGE,
            name=top,
            language=language,
            unit=EXTERNALS_UNIT,
            location=Location(path=EXTERNALS_UNIT, start_line=0, end_line=0),
            provenance=Provenance.LSP,
            confidence=Confidence.EXTRACTED,
            content_hash=_hash(top),
            visibility=Visibility.PUBLIC,
        )
    return external_nodes[key]


# --------------------------------------------------------------------------- #
# Node construction
# --------------------------------------------------------------------------- #


def _make_module_node(
    repo_id: str, module_qname: str, rel_path: str, source: str, language: Language
) -> Node:
    return Node.create(
        repo_id=repo_id,
        qualified_name=module_qname,
        kind=NodeKind.MODULE,
        name=module_qname.rsplit(".", 1)[-1],
        unit=rel_path,
        module=module_qname,
        language=language,
        location=Location(path=rel_path, start_line=1, end_line=source.count("\n") + 1),
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=_hash(source),
        visibility=Visibility.EXPORTED,
    )


def _make_symbol_node(
    repo_id: str,
    sym: RawSymbol,
    rel_path: str,
    *,
    is_top_level: bool,
    language: Language,
    stmt_ordinal: int | None = None,
) -> Node:
    # Explicit visibility (TypeScript export/private) wins; else derive from the name (Python).
    visibility = sym.visibility if sym.visibility is not None else _visibility(sym.name)
    return Node.create(
        repo_id=repo_id,
        qualified_name=sym.qualified_name,
        kind=NodeKind.SYMBOL,
        name=sym.name,
        unit=rel_path,
        language=language,
        signature=sym.signature,
        location=Location(path=rel_path, start_line=sym.start_line, end_line=sym.end_line),
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=_hash(sym.signature),
        body_hash=sym.body_hash,
        visibility=visibility,
        is_top_level=is_top_level,
        is_type_decl=sym.is_type_decl,
        stmt_ordinal=stmt_ordinal,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _enclosing_source(candidates: list[Node] | None, line: int) -> Node | None:
    """The enclosing symbol node for a name occurrence at ``line``.

    A qname usually maps to one symbol. For overloaded methods that share a qname, pick the
    overload whose body (location range) contains the line, so a call inside one overload is not
    sourced from another. Falls back to the first candidate if none contains the line.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for node in candidates:
        if node.location.start_line <= line <= node.location.end_line:
            return node
    return candidates[0]


def _visibility(name: str) -> Visibility:
    return Visibility.INTERNAL if name.startswith("_") else Visibility.EXPORTED


def _hash(text: str) -> str:
    return blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
