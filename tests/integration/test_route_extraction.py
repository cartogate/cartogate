"""ROUTE extraction — Next.js file-routes + React Router + Vue Router literals.

EXTRACTED means extracted: every asserted fact traces to a file path or a string
literal; computed paths are skipped, never guessed. LINKS_TO is advisory-only —
it must never join the gate's edge set (pinned in test_traversal.py).
"""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.schema.enums import Confidence, EdgeType, NodeKind
from cartogate.store.memory import InMemoryStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


def _index(root: Path, store: InMemoryStore | None = None):
    store = store if store is not None else InMemoryStore()
    return index_package(
        root, repo_id="routes-test", store=store, base=root, index_docs=False
    )


def _routes(result) -> dict[str, object]:
    return {
        n.qualified_name: n for n in result.nodes if n.kind is NodeKind.ROUTE
    }


def _links(result) -> set[tuple[str, str]]:
    by_id = {n.id: n for n in result.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in result.edges
        if e.type is EdgeType.LINKS_TO
    }


class TestNextJsFileRoutes:
    def test_app_router_patterns_extracted(self) -> None:
        result = _index(FIXTURES / "nextjs-app")
        routes = _routes(result)
        assert set(routes) == {"/", "/items", "/items/:id", "/about"}

    def test_route_nodes_are_extracted_provenance_with_source_unit(self) -> None:
        result = _index(FIXTURES / "nextjs-app")
        routes = _routes(result)
        detail = routes["/items/:id"]
        assert detail.confidence is Confidence.EXTRACTED
        assert detail.unit == "app/items/[id]/page.tsx"
        about = routes["/about"]  # route group "(marketing)" elided from the url
        assert about.unit == "app/(marketing)/about/page.tsx"

    def test_links_to_from_literal_hrefs(self) -> None:
        result = _index(FIXTURES / "nextjs-app")
        links = _links(result)
        # href="/items" from the "/" page:
        assert ("/", "/items") in links
        # href="/items/1" resolves to the "/items/:id" pattern segment-wise:
        assert ("/items", "/items/:id") in links

    def test_links_to_is_never_a_gate_edge(self) -> None:
        from cartogate.engine.traversal import GATE_EDGE_TYPES

        assert EdgeType.LINKS_TO not in GATE_EDGE_TYPES


class TestReactRouterLiterals:
    def test_jsx_route_paths_and_router_objects_extracted(self) -> None:
        result = _index(FIXTURES / "react-router")
        routes = _routes(result)
        assert set(routes) == {"/", "/users/:userId", "/settings"}

    def test_computed_path_is_skipped_not_guessed(self) -> None:
        result = _index(FIXTURES / "react-router")
        assert "/computed" not in _routes(result)

    def test_path_property_outside_router_context_is_not_a_route(self) -> None:
        # router.js declares notARoute = {path: "/etc/passwd"} outside
        # createBrowserRouter — extraction must not claim it.
        result = _index(FIXTURES / "react-router")
        assert "/etc/passwd" not in _routes(result)

    def test_multi_route_file_links_are_skipped_deterministically(self) -> None:
        # App.jsx declares TWO routes; a <Link> inside it has no unique source
        # route, so v1 emits no LINKS_TO rather than guessing.
        result = _index(FIXTURES / "react-router")
        assert _links(result) == set()


class TestVueRouterLiterals:
    def test_createRouter_route_objects_extracted(self) -> None:
        result = _index(FIXTURES / "vue-router")
        # Relative children JOIN their parent (corpus round, 2026-07-21): the
        # nesting is statically present in the same literal tree — joining is
        # reading, not guessing. "reports" under "/admin" -> "/admin/reports".
        assert set(_routes(result)) == {
            "/", "/products/:pid", "/admin", "/admin/audit", "/admin/reports"
        }

    def test_bare_relative_path_never_becomes_a_route_alone(self) -> None:
        # The join happens through the tree; a naked relative segment must
        # never surface as its own route.
        result = _index(FIXTURES / "vue-router")
        assert "reports" not in _routes(result)

    def test_path_property_outside_createRouter_is_not_a_route(self) -> None:
        result = _index(FIXTURES / "vue-router")
        assert "/etc/shadow" not in _routes(result)


class TestStoreLayerIntegrity:
    """The STORE must hold both structural and route facts for dual-role files.

    Inspector Critical (2026-07-20): bulk_load is last-writer-wins per unit —
    appending route nodes under the same unit key as the structural pass
    silently clobbered module/symbol nodes for every route-declaring file,
    blinding check_duplicate. Asserting on IndexResult (the pre-store
    accumulator) cannot catch this; these tests read the store back.
    """

    def test_symbols_survive_alongside_routes_in_the_store(self) -> None:
        store = InMemoryStore()
        _index(FIXTURES / "react-router", store)
        kinds_by_unit: dict[str, set[NodeKind]] = {}
        for unit, nodes, _edges in store.iter_unit_facts():
            for n in nodes:
                kinds_by_unit.setdefault(unit, set()).add(n.kind)
        app_kinds = kinds_by_unit.get("src/App.jsx", set())
        # App.jsx defines a module, symbols (App, Home, ...) AND two routes —
        # all must be in the store, not just in the returned accumulator.
        assert NodeKind.MODULE in app_kinds
        assert NodeKind.SYMBOL in app_kinds
        assert NodeKind.ROUTE in app_kinds

    def test_all_declared_routes_reach_the_store(self) -> None:
        store = InMemoryStore()
        _index(FIXTURES / "react-router", store)
        stored_routes = {
            n.qualified_name
            for _unit, nodes, _edges in store.iter_unit_facts()
            for n in nodes
            if n.kind is NodeKind.ROUTE
        }
        assert stored_routes == {"/", "/users/:userId", "/settings"}


class TestNextJsPagesTree:
    def test_legacy_pages_routes_extracted(self) -> None:
        result = _index(FIXTURES / "nextjs-pages")
        assert set(_routes(result)) == {"/", "/items/:id"}


class TestReactRouterNestedJoins:
    """Corpus round (2026-07-21): children-relative nesting is the DOMINANT
    real-world pattern — web-mockup extracted 1/15, paperclip 0/94 under the
    absolute-only rule. Within one literal tree the parent chain is a static
    fact; joining it is extraction, not inference."""

    def test_object_literal_children_join_their_parents(self) -> None:
        result = _index(FIXTURES / "rr-nested")
        routes = set(_routes(result))
        assert {"/", "/dashboard", "/admin", "/admin/users",
                "/admin/users/:userId"} <= routes

    def test_absolute_child_stands_alone(self) -> None:
        result = _index(FIXTURES / "rr-nested")
        assert "/reports" in _routes(result)
        assert "/admin/reports" not in _routes(result)

    def test_catchall_and_computed_subtrees_are_skipped(self) -> None:
        result = _index(FIXTURES / "rr-nested")
        routes = set(_routes(result))
        assert not any("*" in r for r in routes)
        assert "/computed" not in routes  # computed parent: unresolvable
        assert "/computed/sub" not in routes  # ...and its relative children
        assert "sub" not in routes

    def test_nested_jsx_routes_join(self) -> None:
        result = _index(FIXTURES / "rr-nested")
        routes = set(_routes(result))
        assert {"/shop", "/shop/cart", "/shop/checkout/:step"} <= routes


class TestNextJsSrcLayout:
    def test_src_app_tree_is_extracted_with_corroboration(self) -> None:
        # ergio-designer shape (corpus): the standard src/app layout extracted
        # ZERO routes — parts[0] had to be app/pages. Corroborated slicing
        # (package.json/next.config sibling of the app dir's parent) fixes it
        # without opening the docs/app false-positive class.
        result = _index(FIXTURES / "nextjs-src")
        assert set(_routes(result)) == {"/reports/:reportId", "/settings"}


class TestStandaloneFallback:
    def test_allow_none_extracts_through_the_pruned_walk(self) -> None:
        # The allow=None branch (non-git tree / git unavailable) goes through
        # the same hardened iter_files walk — inspector High round 2 found it
        # raw-rglobbing with no coverage.
        from cartogate.extract.routes_js import extract_route_facts

        root = FIXTURES / "nextjs-pages"
        facts = extract_route_facts(
            root, repo_id="routes-test", base=root, allow=None
        )
        assert {n.qualified_name for n in facts.nodes} == {"/", "/items/:id"}


class TestPagesDirFalsePositive:
    def test_react_components_pages_dir_is_not_a_route_tree(self) -> None:
        # Corpus round 2 (2026-07-21): graforge/ai-meal-planner shaped —
        # src/pages/ holding COMPONENTS in a plain React app minted phantom
        # EXTRACTED routes (/EditorPage, even /__tests__/...) because ANY
        # package.json corroborated. Corroboration now requires NEXT evidence
        # (next.config.* or a package.json depending on next).
        result = _index(FIXTURES / "react-src-pages")
        assert set(_routes(result)) == set()


class TestWalkerRobustness:
    def test_conditional_jsx_route_inside_matched_parent_joins(self) -> None:
        # {cond && <Route path="promo"/>} nested in <Route path="/shop"> —
        # inspector Medium: the descent whitelist silently skipped conditional
        # rendering, worst inside an already-matched Route.
        result = _index(FIXTURES / "rr-nested")
        assert "/shop/promo" in _routes(result)

    def test_nested_monorepo_src_layout_extracts(self) -> None:
        # apps/web/src/app with next evidence at apps/web (inspector Medium):
        # the src-layout rule only looked at the repo root.
        result = _index(FIXTURES / "monorepo")
        assert "/tools" in _routes(result)

    def test_pathological_nesting_never_crashes_extraction(self) -> None:
        # inspector High: the recursive walkers dropped _walk()'s stack
        # safety — a ~3000-deep routes tree crashed the WHOLE extraction pass
        # with RecursionError, violating "skip, never crash".
        from cartogate.extract.routes_js import _PARSER, _router_path_declarations

        depth = 3000
        src = 'import { createBrowserRouter } from "react-router-dom";\n'
        src += "export const r = createBrowserRouter(["
        src += '{path: "/deep", children: [' * depth
        src += '{path: "leaf"}'
        src += "]}" * depth
        src += "]);\n"
        source = src.encode()
        out = _router_path_declarations(_PARSER.parse(source), source)
        assert ("/deep", 2) in [(p, line) for p, line in out][:1] or any(
            p == "/deep" for p, _ in out
        )


class TestNextDynamicSegments:
    def test_single_dynamic_segment(self) -> None:
        from cartogate.extract.routes_js import _nextjs_pattern

        assert _nextjs_pattern(("app", "items", "[id]", "page.tsx")) == "/items/:id"

    def test_catchall_segment_maps_to_clean_param(self) -> None:
        from cartogate.extract.routes_js import _nextjs_pattern

        assert _nextjs_pattern(("app", "blog", "[...slug]", "page.tsx")) == "/blog/:slug"

    def test_optional_catchall_segment_maps_to_clean_param(self) -> None:
        # [[...slug]] (optional catch-all) previously emitted a malformed
        # "/docs/:[...slug]" — the outer brackets and spread dots are not URL.
        from cartogate.extract.routes_js import _nextjs_pattern

        pattern = _nextjs_pattern(("app", "docs", "[[...slug]]", "page.tsx"))
        assert pattern == "/docs/:slug"
        assert "[" not in pattern and "]" not in pattern


class TestEmptyIndexRoute:
    def test_empty_child_path_is_parent_index_not_slash_dup(self) -> None:
        # {path: "/admin", children: [{path: "", ...}]} — the empty child is
        # the /admin index, NOT a phantom "/admin/".
        from cartogate.extract.routes_js import _PARSER, _router_path_declarations

        src = (
            'import { createBrowserRouter } from "react-router-dom";\n'
            "export const r = createBrowserRouter([\n"
            '  { path: "/admin", children: [\n'
            '    { path: "", element: null },\n'
            '    { path: "users", element: null },\n'
            "  ] },\n"
            "]);\n"
        )
        source = src.encode()
        patterns = {p for p, _ in _router_path_declarations(_PARSER.parse(source), source)}
        assert "/admin" in patterns
        assert "/admin/users" in patterns
        assert "/admin/" not in patterns


class TestHashRouter:
    def test_hash_router_patterns_carry_the_fragment(self) -> None:
        # createHashRouter serves routes under '#': the navigable url for
        # "/about" is "/#/about", not "/about" (which would 404 in a hash app).
        from cartogate.extract.routes_js import _PARSER, _router_path_declarations

        src = (
            'import { createHashRouter } from "react-router-dom";\n'
            "export const r = createHashRouter([\n"
            '  { path: "/about", element: null },\n'
            "]);\n"
        )
        source = src.encode()
        patterns = {p for p, _ in _router_path_declarations(_PARSER.parse(source), source)}
        assert "/#/about" in patterns
        assert "/about" not in patterns

    def test_browser_router_patterns_are_unprefixed(self) -> None:
        from cartogate.extract.routes_js import _PARSER, _router_path_declarations

        src = (
            'import { createBrowserRouter } from "react-router-dom";\n'
            "export const r = createBrowserRouter([\n"
            '  { path: "/about", element: null },\n'
            "]);\n"
        )
        source = src.encode()
        patterns = {p for p, _ in _router_path_declarations(_PARSER.parse(source), source)}
        assert "/about" in patterns
        assert "/#/about" not in patterns


class TestFirstDeclarationWins:
    def test_duplicate_pattern_attributes_to_first_occurrence(self) -> None:
        # Re-scan Low: reversed sibling emission attributed a duplicated
        # pattern's node to its LATER line, violating "first declaration wins".
        from cartogate.extract.routes_js import _PARSER, _router_path_declarations

        src = (
            'import { createBrowserRouter } from "react-router-dom";\n'
            "export const r = createBrowserRouter([\n"
            '  { path: "/first", element: null },\n'
            '  { path: "/first", element: null },\n'
            "]);\n"
        )
        source = src.encode()
        out = _router_path_declarations(_PARSER.parse(source), source)
        firsts = [line for pattern, line in out if pattern == "/first"]
        assert firsts[0] == 3  # first declaration, first in emission order
