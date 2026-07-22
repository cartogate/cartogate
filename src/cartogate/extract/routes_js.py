"""ROUTE extraction: framework route declarations → route nodes + links_to edges.

Stage 2B (nav freshness — spec §3a): Next.js file-based routes and React Router
path literals become ``NodeKind.ROUTE`` nodes with ``EdgeType.LINKS_TO`` edges
from literal ``href``/``to`` usage. EXTRACTED means extracted: every fact traces
to a file path or a string literal in source; computed paths are skipped, never
guessed. ``LINKS_TO`` is deliberately absent from the gate's edge set (R7) —
route facts feed the navmap seed and the drift advisory, never a BLOCK.

Modeled on the doc pass (:mod:`cartogate.extract.docs`): a bolt-on pass invoked
by the pipeline after the structural walk, returning its own nodes/edges.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path

import tree_sitter
import tree_sitter_typescript

from cartogate.extract.walk import iter_files
from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node

_TSX = tree_sitter.Language(tree_sitter_typescript.language_tsx())
_PARSER = tree_sitter.Parser(_TSX)

_PAGE_STEMS = frozenset({"page"})
_ROUTE_SUFFIXES = frozenset({".tsx", ".jsx", ".ts", ".js"})
_ROUTER_IMPORT_RE = re.compile(r"""from\s+['"](?:react-router|vue-router)""")


@dataclass
class RouteFacts:
    """Route nodes + links_to edges produced from a source tree."""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _nextjs_pattern(rel_parts: tuple[str, ...]) -> str | None:
    """URL pattern for an ``app/**/page.*`` or ``pages/**`` file, else None.

    Mappings (documented, nothing else): ``[seg]`` → ``:seg``; route groups
    ``(group)`` elided; ``index``/``page`` stems terminate the path.
    """
    if not rel_parts:
        return None
    tree, rest = rel_parts[0], rel_parts[1:]
    if tree == "app":
        if not rest or Path(rest[-1]).stem not in _PAGE_STEMS:
            return None
        segments = rest[:-1]
    elif tree == "pages":
        if not rest:
            return None
        stem = Path(rest[-1]).stem
        segments = rest[:-1] if stem == "index" else rest[:-1] + (stem,)
    else:
        return None

    parts: list[str] = []
    for seg in segments:
        if seg.startswith("(") and seg.endswith(")"):
            continue  # route group — organizational only, elided from the URL
        if seg.startswith("[") and seg.endswith("]"):
            # [id] -> :id ; catch-all [...slug] and optional catch-all
            # [[...slug]] both collapse to a single :slug param — the (possibly
            # doubled) brackets and the spread dots are not part of the URL.
            parts.append(":" + seg.strip("[]").lstrip("."))
        else:
            parts.append(seg)
    return "/" + "/".join(parts) if parts else "/"


_NEXT_CONFIGS = (
    "next.config.js", "next.config.ts", "next.config.mjs", "next.config.cjs",
)


def _has_next_evidence(directory: Path) -> bool:
    """Whether ``directory`` is a NEXT.JS project root — not merely a JS one.

    Corpus round 2 (2026-07-21): accepting ANY package.json minted phantom
    routes from src/pages/ COMPONENT folders in plain React apps (graforge's
    /EditorPage, ai-meal-planner's /__tests__/... — false EXTRACTED facts).
    Evidence means a next.config.* file or a package.json that actually
    depends on "next".
    """
    import json

    try:
        if any((directory / cfg).is_file() for cfg in _NEXT_CONFIGS):
            return True
        pkg = directory / "package.json"
        if not pkg.is_file():
            return False
        data = json.loads(pkg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        for section in ("dependencies", "devDependencies"):
            deps = data.get(section)
            if isinstance(deps, dict) and "next" in deps:
                return True
    except (OSError, ValueError):
        return False
    return False


def _nextjs_pattern_for_tree(rel_parts: tuple[str, ...], root: Path) -> str | None:
    """Next.js pattern for a file path under ``root`` — Next-evidence required.

    The app/pages segment counts only when its project root shows NEXT
    evidence: the root itself, the root for the standard ``src/`` layout
    (corpus: ergio-designer's src/app extracted ZERO routes before), or the
    segment's parent dir for nested monorepo apps. A components dir that
    merely LOOKS like a pages tree stays silent (see _has_next_evidence).
    """
    for i, seg in enumerate(rel_parts):
        if seg not in ("app", "pages"):
            continue
        base_parts = rel_parts[:i]
        if base_parts and base_parts[-1] == "src":
            base_parts = base_parts[:-1]  # standard src/ layout at ANY depth
        project_dir = root.joinpath(*base_parts) if base_parts else root
        if not _has_next_evidence(project_dir):
            continue
        pattern = _nextjs_pattern(rel_parts[i:])
        if pattern is not None:
            return pattern
    return None


def _string_literal(node: tree_sitter.Node, source: bytes) -> str | None:
    """The unquoted text of a plain string literal node, else None."""
    if node.type != "string":
        return None
    # A "string" with substitutions (template) never parses as type "string"
    # in this grammar; fragments are the quoted content.
    frags = [c for c in node.children if c.type == "string_fragment"]
    if len(frags) != 1:
        return "" if not frags else None
    return source[frags[0].start_byte : frags[0].end_byte].decode("utf-8", "replace")


def _walk(node: tree_sitter.Node) -> Iterator[tree_sitter.Node]:
    """Depth-first pre-order walk in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _jsx_attr_literal(
    element: tree_sitter.Node, attr_name: str, source: bytes
) -> tuple[str, int] | None:
    """(value, line) of a literal JSX attribute on ``element``, else None."""
    for child in element.children:
        if child.type != "jsx_attribute":
            continue
        ident = child.child(0)
        if ident is None or source[ident.start_byte : ident.end_byte] != attr_name.encode():
            continue
        for sub in child.children:
            value = _string_literal(sub, source)
            if value is not None:
                return value, child.start_point[0] + 1
    return None


_ROUTER_CALLS = (
    b"createBrowserRouter", b"createHashRouter", b"useRoutes", b"createRouter",
)


def _join_route(parent: str | None, path: str) -> str | None:
    """Join a route path onto its parent chain — a STATIC fact, not a guess.

    Absolute paths stand alone; catch-alls (any ``*`` segment) are not
    navigable url patterns; relative paths need a resolved parent — an
    unresolvable chain (computed ancestor) poisons its relative descendants.
    An empty path is an INDEX route: it resolves to the parent's own url (root
    "/" with no parent), never a phantom ``parent + "/"`` (review 2026-07-22).
    """
    if path == "":
        return parent if parent is not None else "/"
    if any(seg == "*" or seg.endswith("*") for seg in path.split("/")):
        return None
    if path.startswith("/"):
        return path
    if parent is None:
        return None
    return parent.rstrip("/") + "/" + path


def _object_pair(obj: tree_sitter.Node, key: bytes, source: bytes) -> tree_sitter.Node | None:
    """The value node of a direct ``key:`` pair on an object literal, else None."""
    for child in obj.children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")
        if key_node is None:
            continue
        if source[key_node.start_byte : key_node.end_byte] in (key, b'"' + key + b'"'):
            return child.child_by_field_name("value")
    return None


def _hashify(pattern: str) -> str:
    """A HashRouter serves routes under '#': ``/about`` navigates to ``/#/about``.

    ``pattern`` always starts with "/", so ``/#`` + pattern is the navigable
    url; root ``/`` becomes ``/#/``.
    """
    return "/#" + pattern


def _walk_route_objects(
    array_node: tree_sitter.Node,
    parent: str | None,
    source: bytes,
    out: list[tuple[str, int]],
    *,
    hash_router: bool = False,
) -> None:
    """Iteratively extract joined paths from a routes array literal.

    Corpus round (2026-07-21): children-relative nesting is the dominant
    real-world pattern. Within one literal tree the parent chain is statically
    present — joining is reading. A pathless route (layout/index) passes the
    parent through. Explicit stack, not recursion: a pathological ~3000-deep
    tree must be handled, never crash the extraction pass (inspector High —
    the recursive version dropped _walk()'s stack safety).

    ``hash_router`` prefixes each EMITTED pattern with ``#`` (createHashRouter):
    joining stays in plain path space so children resolve normally; the
    fragment is a serving concern applied once at emission (review 2026-07-22).
    """
    stack: list[tuple[tree_sitter.Node, str | None]] = [(array_node, parent)]
    while stack:
        current_array, current_parent = stack.pop()
        # Emit siblings in FORWARD document order (first declaration wins in
        # the caller's dedup — re-scan Low: a reversed emission loop attributed
        # duplicate patterns to their LATER occurrence); child arrays are then
        # pushed reversed so pops preserve document order across levels.
        pending_children: list[tuple[tree_sitter.Node, str | None]] = []
        for obj in current_array.children:
            if obj.type != "object":
                continue
            path_value = _object_pair(obj, b"path", source)
            if path_value is None:
                joined: str | None = current_parent  # pathless layout route
            else:
                literal = _string_literal(path_value, source)
                joined = (
                    None if literal is None else _join_route(current_parent, literal)
                )
                if joined is not None:
                    emitted = _hashify(joined) if hash_router else joined
                    out.append((emitted, path_value.start_point[0] + 1))
            children = _object_pair(obj, b"children", source)
            if children is not None and children.type == "array":
                pending_children.append((children, joined))
        stack.extend(reversed(pending_children))


def _jsx_element_name(node: tree_sitter.Node, source: bytes) -> bytes | None:
    opening = node if node.type == "jsx_self_closing_element" else node.child(0)
    if opening is None:
        return None
    name = opening.child_by_field_name("name")
    if name is None:
        return None
    return source[name.start_byte : name.end_byte]


def _walk_jsx_routes(
    root_node: tree_sitter.Node,
    source: bytes,
    out: list[tuple[str, int]],
) -> None:
    """Iteratively extract joined paths from nested ``<Route>`` JSX trees.

    Full descent with an explicit (node, parent-path) stack: conditional
    rendering, fragments, and callback bodies inside a matched Route all join
    correctly (inspector Medium — a descent whitelist silently skipped them),
    and pathological nesting cannot crash the pass.
    """
    stack: list[tuple[tree_sitter.Node, str | None]] = [(root_node, None)]
    while stack:
        node, parent = stack.pop()
        child_parent = parent
        if (
            node.type in ("jsx_element", "jsx_self_closing_element")
            and _jsx_element_name(node, source) == b"Route"
        ):
            opening = (
                node if node.type == "jsx_self_closing_element" else node.child(0)
            )
            found = _jsx_attr_literal(opening, "path", source) if opening else None
            if found is not None:
                joined = _join_route(parent, found[0])
                if joined is not None:
                    out.append((joined, found[1]))
                child_parent = joined
            # pathless/index Route: child_parent stays as parent (pass-through)
        for child in reversed(node.children):
            stack.append((child, child_parent))


def _router_path_declarations(
    tree: tree_sitter.Tree, source: bytes
) -> list[tuple[str, int]]:
    """(pattern, line) for every statically resolvable route in a router file.

    Shapes: nested ``<Route path>`` JSX trees (React Router) and routes-array
    object literals inside createBrowserRouter/createHashRouter/useRoutes
    (React Router) or createRouter (Vue Router) — relative children JOIN
    their parent chain (a static fact of the same literal tree). A ``path:``
    property anywhere else is NOT a route; computed paths poison their
    relative descendants; catch-alls are skipped. EXTRACTED means extracted.
    """
    out: list[tuple[str, int]] = []
    _walk_jsx_routes(tree.root_node, source, out)
    for node in _walk(tree.root_node):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or source[fn.start_byte : fn.end_byte] not in _ROUTER_CALLS:
            continue
        is_hash = source[fn.start_byte : fn.end_byte] == b"createHashRouter"
        args = node.child_by_field_name("arguments")
        if args is None:
            continue
        for arg in args.children:
            if arg.type == "array":
                _walk_route_objects(arg, None, source, out, hash_router=is_hash)
            elif arg.type == "object":
                routes = _object_pair(arg, b"routes", source)
                if routes is not None and routes.type == "array":
                    _walk_route_objects(routes, None, source, out, hash_router=is_hash)
    return out


def _link_literals(tree: tree_sitter.Tree, source: bytes) -> list[tuple[str, int]]:
    """(target, line) for every literal ``href=``/``to=`` JSX attribute."""
    out: list[tuple[str, int]] = []
    for node in _walk(tree.root_node):
        if node.type not in ("jsx_element", "jsx_self_closing_element"):
            continue
        opening = node if node.type == "jsx_self_closing_element" else node.child(0)
        if opening is None:
            continue
        for attr in ("href", "to"):
            found = _jsx_attr_literal(opening, attr, source)
            if found is not None and found[0].startswith("/"):
                out.append(found)
    return out


def _href_matches_pattern(href: str, pattern: str) -> bool:
    """Segment-wise match: a ``:param`` segment matches any literal segment."""
    href_segs = href.rstrip("/").split("/")
    pat_segs = pattern.rstrip("/").split("/")
    if len(href_segs) != len(pat_segs):
        return False
    return all(
        p.startswith(":") or p == h for p, h in zip(pat_segs, href_segs, strict=True)
    )


def extract_route_facts(
    root: Path,
    *,
    repo_id: str,
    base: Path,
    allow: list[Path] | None = None,
) -> RouteFacts:
    """Extract route nodes + links_to edges from JS/TS sources under ``root``."""
    facts = RouteFacts()

    # Both branches (allow given or not) go through the pipeline's pruned,
    # exception-hardened walk — an independent rglob reintroduced the
    # node_modules crash class the pipeline explicitly prunes, first on the
    # main path and then again in the allow=None fallback (inspector High,
    # 2026-07-20, two rounds). Mirrors docs.py exactly.
    candidates = sorted(
        path
        for suffix in sorted(_ROUTE_SUFFIXES)
        for path in iter_files(root, suffix, allow)
    )

    root_resolved = root.resolve()
    base_resolved = base.resolve()

    # (pattern, rel_path, line) declarations in deterministic path order.
    declarations: list[tuple[str, str, int]] = []
    links_by_file: dict[str, list[tuple[str, int]]] = {}

    for path in candidates:
        # Resilient to one bad file, like the structural pass: skip, never crash.
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            rel = resolved.relative_to(base_resolved).as_posix()
            rel_parts = resolved.relative_to(root_resolved).parts
            source = path.read_bytes()
        except (OSError, ValueError):
            continue

        pattern = _nextjs_pattern_for_tree(rel_parts, root_resolved)
        if pattern is not None:
            declarations.append((pattern, rel, 1))

        text = source.decode("utf-8", "replace")
        has_router_import = _ROUTER_IMPORT_RE.search(text) is not None
        if not has_router_import and b"href=" not in source:
            continue
        tree = _PARSER.parse(source)
        if has_router_import:
            for decl_pattern, line in _router_path_declarations(tree, source):
                declarations.append((decl_pattern, rel, line))
        links_by_file.setdefault(rel, []).extend(_link_literals(tree, source))

    # One node per unique pattern; first declaration (path order) wins.
    nodes_by_pattern: dict[str, Node] = {}
    routes_by_file: dict[str, list[str]] = {}
    for pattern, rel, line in declarations:
        routes_by_file.setdefault(rel, []).append(pattern)
        if pattern in nodes_by_pattern:
            continue
        node = Node.create(
            repo_id=repo_id,
            qualified_name=pattern,
            kind=NodeKind.ROUTE,
            name=pattern,
            unit=rel,
            location=Location(path=rel, start_line=line, end_line=line),
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            content_hash=blake2b(
                f"{pattern}\x00{rel}".encode(), digest_size=16
            ).hexdigest(),
            visibility=Visibility.PUBLIC,
        )
        nodes_by_pattern[pattern] = node
        facts.nodes.append(node)

    # links_to: only when the linking file declares exactly ONE route (a unique
    # source); otherwise skipped — deterministic, never guessed.
    for rel, links in sorted(links_by_file.items()):
        file_routes = routes_by_file.get(rel, [])
        if len(file_routes) != 1:
            continue
        src_node = nodes_by_pattern[file_routes[0]]
        for target, line in links:
            for pattern, dst_node in nodes_by_pattern.items():
                if _href_matches_pattern(target, pattern):
                    facts.edges.append(
                        Edge(
                            type=EdgeType.LINKS_TO,
                            src=src_node.id,
                            dst=dst_node.id,
                            provenance=Provenance.TREE_SITTER,
                            confidence=Confidence.EXTRACTED,
                            source_location=SourceLocation(path=rel, line=line),
                        )
                    )
                    break
    return facts
