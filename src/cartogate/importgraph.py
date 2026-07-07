"""Lightweight Python module-import graph for the new-cycle advisory (STRATEGY.md Phase 2).

The gate's index is deliberately resolution-free (fast, fail-closed), so it has no ``imports``
edges — and running the full resolver at commit time would take minutes on large repos, which
trains agents to bypass. This module builds the module-level import topology STRUCTURALLY:
tree-sitter import statements only, mapped against the repo's own module set, no name
resolution, milliseconds per file. Python-only v1 (the dotted-module mapping is
language-specific); deterministic by construction.

Soundness posture (advisory): edges are exact for literal ``import``/``from`` statements.
Blind spots, enumerated: dynamic imports (``importlib``, ``__import__``) are invisible — a
cycle through them is missed, never invented; ``package_dir`` remappings other than a top-level
``src/`` root are unmapped (their edges vanish -> cycles missed, never invented); on a repo
whose single tangled SCC holds more than the per-graph cycle cap, old/new enumeration can crowd
differently and misreport (cap raised only if field evidence demands).
"""

from __future__ import annotations

import functools

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

_PY = Language(tspython.language())


def module_name_for(rel_path: str) -> str:
    """Dotted module name for a repo-relative POSIX ``.py`` path (``pkg/__init__.py`` -> pkg).

    A leading ``src/`` root is stripped (the PyPA-recommended src layout): imports say
    ``cartogate.x``, never ``src.cartogate.x`` — without this the whole graph silently loses
    its edges on src-layout repos (including this one). Other ``package_dir`` remappings are
    a documented limitation.
    """
    if rel_path.startswith("src/"):
        rel_path = rel_path[len("src/"):]
    parts = rel_path[: -len(".py")].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _node_text(node: object) -> str:
    text = getattr(node, "text", None)
    return text.decode("utf-8", "replace") if isinstance(text, bytes) else ""


@functools.lru_cache(maxsize=4096)
def _imports_cached(source: str, module: str, is_package: bool) -> tuple[str, ...]:
    return tuple(_python_imports_uncached(source, module, is_package))


def python_imports_in(source: str, *, module: str, is_package: bool = False) -> list[str]:
    """Cached wrapper: the old/new commit-gate graphs share most file contents, so each unique
    (source, module) parses once per process instead of twice.

    ``is_package`` marks a package's own ``__init__.py``: its dotted name already IS the
    containing package for relative-import arithmetic (``from . import x`` in
    ``pkg/sub/__init__.py`` means ``pkg.sub.x``, not ``pkg.x``).
    """
    return list(_imports_cached(source, module, is_package))


def _python_imports_uncached(source: str, module: str, is_package: bool) -> list[str]:
    """Sorted dotted CANDIDATE targets of every literal import statement in ``source``.

    ``from X import Y`` emits both ``X`` and ``X.Y`` (Y may be a module or a symbol — the
    graph builder resolves candidates against the repo's real module set).

    Relative imports resolve against ``module``: in ``pkg.inner.mod``, ``.`` is ``pkg.inner``
    and ``..other`` is ``pkg.other``. Unresolvable over-deep relatives are dropped (a broken
    import can't be a dependency edge).
    """
    tree = Parser(_PY).parse(source.encode("utf-8"))
    targets: set[str] = set()
    # For a plain module the containing package strips one level; a package __init__ IS
    # already its own package (review HIGH-3: the off-by-one broke `from . import x`).
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "if_statement":
            # `if TYPE_CHECKING:` imports exist precisely to BREAK runtime cycles — counting
            # them would accuse the standard fix. Skip the whole block (the else branch of a
            # TYPE_CHECKING guard is exotic enough to ignore).
            condition = node.child_by_field_name("condition")
            if condition is not None and "TYPE_CHECKING" in _node_text(condition):
                continue
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    targets.add(_node_text(child))
                elif child.type == "aliased_import":
                    name = child.child_by_field_name("name")
                    if name is not None:
                        targets.add(_node_text(name))
        elif node.type == "import_from_statement":
            raw = node.child_by_field_name("module_name")
            text = _node_text(raw) if raw is not None else ""
            if text.startswith("."):
                dots = len(text) - len(text.lstrip("."))
                remainder = text.lstrip(".")
                # one dot = the containing package; each extra dot walks one level up
                if len(package_parts) - (dots - 1) < 0:
                    continue  # relative import above the repo root — not an edge
                base = package_parts[: len(package_parts) - (dots - 1)]
                text = ".".join([*base, remainder] if remainder else base)
            if not text:
                continue
            targets.add(text)
            # ``from X import Y`` may import the MODULE X.Y (``from app import a``) — emit the
            # dotted candidates too; the graph builder keeps whichever resolve to real modules.
            # (Skip the module_name child by POSITION: tree-sitter hands out distinct node
            # objects for field lookup vs iteration, so identity comparison would miss it.)
            for child in node.named_children:
                if raw is not None and child.start_byte == raw.start_byte:
                    continue
                if child.type == "dotted_name":
                    targets.add(f"{text}.{_node_text(child)}")
                elif child.type == "aliased_import":
                    name = child.child_by_field_name("name")
                    if name is not None:
                        targets.add(f"{text}.{_node_text(name)}")
        stack.extend(node.named_children)
    return sorted(targets)


def build_import_graph(files: dict[str, str]) -> dict[str, set[str]]:
    """Module dependency graph over repo-internal modules only.

    ``files`` maps repo-relative POSIX ``.py`` paths to their source. An import target counts
    as an edge when it IS a repo module, or when its longest strict prefix is (``from app.b
    import helper`` -> ``app.b``). External imports vanish.
    """
    modules = {
        module_name_for(path): (source, path.endswith("__init__.py"))
        for path, source in files.items()
    }
    known = set(modules)
    graph: dict[str, set[str]] = {name: set() for name in known}
    for name, (source, is_package) in modules.items():
        for target in python_imports_in(source, module=name, is_package=is_package):
            resolved = target
            while resolved and resolved not in known:
                resolved = resolved.rpartition(".")[0]
            if resolved and resolved != name:
                graph[name].add(resolved)
    return graph


def _cycles_of(graph: dict[str, set[str]], *, limit: int = 25) -> set[tuple[str, ...]]:
    """Canonicalized simple cycles (rotation-normalized), bounded for responsiveness."""
    import networkx as nx

    g = nx.DiGraph()
    g.add_nodes_from(sorted(graph))
    for src in sorted(graph):
        for dst in sorted(graph[src]):
            g.add_edge(src, dst)
    cycles: set[tuple[str, ...]] = set()
    for scc in nx.strongly_connected_components(g):
        if len(scc) < 2:
            continue
        for cycle in nx.simple_cycles(g.subgraph(sorted(scc))):
            smallest = min(range(len(cycle)), key=lambda i: cycle[i])
            cycles.add(tuple(cycle[smallest:] + cycle[:smallest]))
            if len(cycles) >= limit:
                return cycles
    return cycles


def find_new_cycles(
    old: dict[str, set[str]], new: dict[str, set[str]], *, limit: int = 25
) -> list[list[str]]:
    """Cycles present in ``new`` but not in ``old`` — what THIS change introduced.

    A pre-existing cycle is never re-accused, even when the change edits a file inside it.
    """
    introduced = _cycles_of(new, limit=limit) - _cycles_of(old, limit=limit)
    return sorted(list(cycle) for cycle in introduced)
