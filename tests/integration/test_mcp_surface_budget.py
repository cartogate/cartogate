"""The MCP surface budget, end to end through ``dispatch``.

``dispatch`` is the one route the MCP server and the daemon share, so what it returns is what an
agent pays for. These tests hold it to ``cartogate.mcp.budget`` on graphs built to be expensive
(one symbol with hundreds of referrers), and pin the other half of the design: the
``CartogateTools`` methods themselves stay full and unpaged, because the pre-commit gate and the
task CLI call them from Python and read every row.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from tests.mcp_budget_support import HUB, hub_store, symbol, wide_store

from cartogate.extract.pipeline import index_package
from cartogate.mcp.budget import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RESPONSE_CHAR_BUDGET,
    RESPONSE_CHAR_CEILING,
    ROW_LIST_CAP,
    UNITS_CAP,
    compact_size,
)
from cartogate.mcp.server import _with_operational_shapes
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore

FIXTURES = Path(__file__).parent.parent / "fixtures"

#: Referrers of the hub in ``hub_store()``: 300 callers and 60 tests, in 120 files.
_REFERRERS = 360
_REFERRER_FILES = 120

_SPECS = {spec["name"]: spec for spec in TOOL_SPECS}


@pytest.fixture(scope="module")
def hub_tools() -> CartogateTools:
    """Tools over the hub graph; built once, since nothing here writes to the store."""
    return CartogateTools(hub_store())


def test_python_api_is_full_and_unpaged(hub_tools: CartogateTools) -> None:
    refs = hub_tools.find_references(HUB)["references"]

    assert len(refs) == _REFERRERS
    assert all("unit" in row and "sites" in row and "signature" in row for row in refs)


@pytest.mark.parametrize(
    ("name", "args", "rows_key"),
    [
        ("find_references", {"qualified_name": HUB}, "references"),
        ("blast_radius", {"symbol": HUB}, "affected"),
        ("blast_radius", {"symbol": HUB, "depth": 3}, "affected"),
    ],
)
def test_default_responses_fit_the_budget(
    hub_tools: CartogateTools, name: str, args: dict[str, Any], rows_key: str
) -> None:
    result = dispatch(hub_tools, name, args)

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert result["count"] == _REFERRERS and result["truncated"] is True
    assert result["returned"] == len(result[rows_key]) <= DEFAULT_LIMIT
    assert result["next_offset"] == result["returned"]
    assert result["unit_count"] == _REFERRER_FILES and len(result["units"]) == UNITS_CAP


def test_presented_reference_rows_carry_the_path_once(hub_tools: CartogateTools) -> None:
    rows = dispatch(hub_tools, "find_references", {"qualified_name": HUB})["references"]

    first = rows[0]
    assert first["qualified_name"] == "pkg.mod_000.caller_000"
    assert first["location"] == "pkg/mod_000.py:10"
    assert first["lines"] == [11, 12, 13, 14, 15] and first["more_lines"] == 3
    assert "unit" not in first and "sites" not in first and "signature" not in first


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("find_references", {"qualified_name": HUB}),
        ("find_references", {"qualified_name": "zz_missing"}),
        ("blast_radius", {"symbol": HUB}),
        ("blast_radius", {"symbol": "zz_missing"}),
        ("implementations", {"qualified_name": HUB}),
        ("implementations", {"qualified_name": "zz_missing"}),
    ],
)
def test_results_validate_against_the_advertised_schema(
    hub_tools: CartogateTools, name: str, args: dict[str, Any]
) -> None:
    result = dispatch(hub_tools, name, args)
    schema = _SPECS[name]["output_schema"]

    jsonschema.validate(result, schema)
    # The lazy server (what `cartogate-mcp` runs) advertises the union with its session payloads.
    jsonschema.validate(result, _with_operational_shapes(schema))


def _indexed(fixture: str) -> tuple[CartogateTools, list[str]]:
    """Tools over one fixture repository, and the qualified name of everything in it."""
    store = InMemoryStore()
    result = index_package(FIXTURES / fixture, repo_id="t", store=store)
    names = [n.qualified_name for n in result.nodes if n.kind in (NodeKind.SYMBOL, NodeKind.MODULE)]
    return CartogateTools(store), names


@pytest.mark.parametrize(
    "fixture",
    ["sample_pkg", "sample_ts", "sample_go", "sample_java", "sample_c", "sample_cpp"],
)
def test_an_indexed_row_repeats_its_file_in_unit_and_location(fixture: str) -> None:
    """The presenter drops ``unit`` on the strength of this: in a real index, in every language
    family, a row's ``unit`` is the path in its ``location``."""
    tools, names = _indexed(fixture)

    rows = [row for name in names for row in tools.find_references(name)["references"]]

    assert rows, f"{fixture} must hold resolved references"
    assert all(row["location"].rsplit(":", 1)[0] == row["unit"] for row in rows)


@pytest.mark.parametrize("fixture", ["sample_c", "sample_cpp"])
def test_a_module_row_keeps_call_sites_that_are_in_another_file(fixture: str) -> None:
    """One module node stands for a C or C++ source file and its header, so its call sites span
    both. Presentation must keep the other file's site, not assume every site is the row's own."""
    tools, names = _indexed(fixture)
    spanning = [
        (name, row)
        for name in names
        for row in tools.find_references(name)["references"]
        if any(site.rsplit(":", 1)[0] != row["unit"] for site in row["sites"])
    ]
    assert spanning, f"{fixture} must hold a module whose references span two files"
    name, raw_row = spanning[0]
    elsewhere = [site for site in raw_row["sites"] if site.rsplit(":", 1)[0] != raw_row["unit"]]

    presented = dispatch(tools, "find_references", {"qualified_name": name})

    referrer = raw_row["qualified_name"]
    row = next(r for r in presented["references"] if r["qualified_name"] == referrer)
    assert row["sites"] == elsewhere
    jsonschema.validate(presented, _SPECS["find_references"]["output_schema"])


def test_dispatch_is_deterministic_to_the_byte(hub_tools: CartogateTools) -> None:
    args = {"qualified_name": HUB, "limit": 50, "offset": 7}

    first = json.dumps(dispatch(hub_tools, "find_references", dict(args)))
    second = json.dumps(dispatch(hub_tools, "find_references", dict(args)))

    assert first == second


# --- paging arguments, end to end -----------------------------------------------------------


def test_paging_visits_every_row_exactly_once(hub_tools: CartogateTools) -> None:
    expected = [row["qualified_name"] for row in hub_tools.find_references(HUB)["references"]]
    seen: list[str] = []
    offset: int | None = 0
    pages = 0

    while offset is not None:
        result = dispatch(hub_tools, "find_references", {"qualified_name": HUB, "offset": offset})
        seen.extend(row["qualified_name"] for row in result["references"])
        offset = result.get("next_offset")
        pages += 1
        assert pages <= _REFERRERS, "paging must make progress"

    assert seen == expected


def test_an_explicit_limit_is_honoured_up_to_the_ceiling(hub_tools: CartogateTools) -> None:
    result = dispatch(hub_tools, "find_references", {"qualified_name": HUB, "limit": 10**6})

    assert DEFAULT_LIMIT < result["returned"] <= MAX_LIMIT
    assert compact_size(result) <= RESPONSE_CHAR_CEILING
    assert result["count"] == _REFERRERS


@pytest.mark.parametrize("junk", [None, "abc", -5, True, [1], {"n": 1}, 2.5])
def test_junk_paging_arguments_never_fail_a_call(hub_tools: CartogateTools, junk: object) -> None:
    result = dispatch(hub_tools, "blast_radius", {"symbol": HUB, "limit": junk, "offset": junk})

    assert result["count"] == _REFERRERS and 1 <= result["returned"] <= DEFAULT_LIMIT
    assert compact_size(result) <= RESPONSE_CHAR_CEILING


def test_cut_suggest_tests_counts_every_test_file_and_lists_the_first(
    hub_tools: CartogateTools,
) -> None:
    result = dispatch(hub_tools, "suggest_tests", {"symbols": [HUB]})

    assert result["count"] == 60 and result["returned"] == DEFAULT_LIMIT
    # The rollup counts all 60 files but lists only the first UNITS_CAP: `unit_count` above
    # `len(units)` is the signal that the list is partial.
    assert result["unit_count"] == 60 and len(result["units"]) == UNITS_CAP < 60
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_paging_suggest_tests_recovers_every_test_file(hub_tools: CartogateTools) -> None:
    files: set[str] = set()
    offset: int | None = 0

    while offset is not None:
        result = dispatch(hub_tools, "suggest_tests", {"symbols": [HUB], "offset": offset})
        files.update(row["unit"] for row in result["tests"])
        offset = result.get("next_offset")

    assert len(files) == 60


def test_impact_summary_cuts_each_list_and_keeps_totals(hub_tools: CartogateTools) -> None:
    raw = hub_tools.impact_summary(symbols=[HUB])

    result = dispatch(hub_tools, "impact_summary", {"symbols": [HUB]})

    assert len(raw["affected"]) > DEFAULT_LIMIT >= len(result["affected"]) >= 1
    assert len(result["tests"]) <= DEFAULT_LIMIT
    assert result["counts"] == raw["counts"] and result["truncated"] is True
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("suggest_tests", {"symbols": [HUB]}),
        ("suggest_tests", {"symbols": ["zz_missing"]}),
        ("doc_drift", {"symbols": [HUB]}),
        ("find_dead_code", {}),
        ("find_duplicate_bodies", {}),
        ("impact_summary", {"symbols": [HUB]}),
        ("impact_summary", {"symbols": ["zz_missing"]}),
        ("localize", {"test": "tests.test_hub_000.test_calls_hub"}),
        ("localize", {"test": "zz_missing"}),
    ],
)
@pytest.mark.parametrize(
    "paging", [{}, {"limit": 7}, {"limit": 200, "offset": 3}, {"offset": 10**6}]
)
def test_paged_results_validate_against_the_advertised_schema(
    hub_tools: CartogateTools, name: str, args: dict[str, Any], paging: dict[str, int]
) -> None:
    result = dispatch(hub_tools, name, {**args, **paging})
    schema = _SPECS[name]["output_schema"]

    jsonschema.validate(result, schema)
    jsonschema.validate(result, _with_operational_shapes(schema))
    assert compact_size(result) <= RESPONSE_CHAR_CEILING


@pytest.mark.parametrize("name", ["suggest_tests", "doc_drift", "impact_summary"])
def test_tools_that_echo_input_declare_the_counts_they_add(name: str) -> None:
    props = _SPECS[name]["output_schema"]["properties"]

    assert props["unresolved_count"] == {"type": "integer"}
    if name != "impact_summary":  # its `counts.changed` already carries the total
        assert props["changed_count"] == {"type": "integer"}


@pytest.mark.parametrize("name", ["suggest_tests", "doc_drift", "impact_summary"])
def test_descriptions_of_cut_lists_say_how_to_get_the_rest(name: str) -> None:
    description = _SPECS[name]["description"]

    assert "truncated" in description


def test_every_paged_tool_advertises_limit_and_offset() -> None:
    paged = {
        "find_references",
        "blast_radius",
        "implementations",
        "suggest_tests",
        "doc_drift",
        "find_dead_code",
        "find_duplicate_bodies",
        "localize",
    }
    for name in paged:
        props = _SPECS[name]["input_schema"]["properties"]
        assert props["limit"]["type"] == "integer" and props["limit"]["default"] == DEFAULT_LIMIT
        assert props["offset"] == {"type": "integer", "minimum": 0}
    impact = _SPECS["impact_summary"]["input_schema"]["properties"]
    assert "limit" in impact and "offset" not in impact


# --- repo_map fits any repository -----------------------------------------------------------


@pytest.fixture(scope="module")
def wide_tools() -> CartogateTools:
    """Tools over a 2,000-file repository (20 areas x 10 packages x 10 files)."""
    return CartogateTools(wide_store())


def _mixed_store() -> InMemoryStore:
    """``app/main.py`` beside thirty ``app/sub_NN/mod.py`` — a directory holding both files and
    subdirectories, in a repository too large to list file by file."""
    store = InMemoryStore()
    index = [("app/main.py", "app.main")] + [
        (f"app/sub_{n:02d}/mod.py", f"app.sub_{n:02d}.mod") for n in range(30)
    ]
    store.bulk_load(
        (unit, [symbol(f"{module}.run", unit, signature="def run():")], [])
        for unit, module in index
    )
    return store


def test_repo_map_overview_collapses_to_directories(wide_tools: CartogateTools) -> None:
    result = dispatch(wide_tools, "repo_map", {})

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert "units" not in result and result["unit_count"] == 2_000
    assert result["depth"] == 2 and result["dir_count"] == 20
    assert result["dirs"][0] == {"dir": "svc/area_00", "units": 100, "symbols": 600}
    assert result["truncated"] is False


def test_repo_map_zooms_into_a_directory(wide_tools: CartogateTools) -> None:
    result = dispatch(wide_tools, "repo_map", {"module": "svc/area_03"})

    assert result["root"] == "svc/area_03" and result["unit_count"] == 100
    assert [d["dir"] for d in result["dirs"]][:2] == ["svc/area_03/pkg_00", "svc/area_03/pkg_01"]
    assert result["dir_count"] == 10 and result["node_count"] == 600


def test_repo_map_lists_files_when_they_fit(wide_tools: CartogateTools) -> None:
    result = dispatch(wide_tools, "repo_map", {"module": "svc/area_03/pkg_04"})

    assert [u["unit"] for u in result["units"]][0] == "svc/area_03/pkg_04/file_000.py"
    assert len(result["units"]) == 10 and "dirs" not in result
    assert result["units"][0]["exports"] == ["fn_0", "fn_1", "fn_2", "fn_3", "fn_4", "fn_5"]
    assert result["units"][0]["symbols"] == 6


def test_repo_map_single_file_repo(tmp_path: Path) -> None:
    (tmp_path / "solo.py").write_text("def only():\n    return 1\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="solo", store=store, base=tmp_path, resolve=False)

    result = dispatch(CartogateTools(store), "repo_map", {})

    assert result["unit_count"] == 1 and [u["unit"] for u in result["units"]] == ["solo.py"]
    assert result["units"][0]["exports"] == ["only"] and "dirs" not in result


def test_repo_map_python_api_is_unchanged(wide_tools: CartogateTools) -> None:
    raw = wide_tools.repo_map()

    assert len(raw["units"]) == 2_000 and isinstance(raw["units"][0]["exports"][0], dict)
    assert "dirs" not in raw and "root" not in raw


def test_every_dir_row_can_be_passed_back_as_module(wide_tools: CartogateTools) -> None:
    overview = dispatch(wide_tools, "repo_map", {})

    for row in overview["dirs"]:
        zoomed = dispatch(wide_tools, "repo_map", {"module": row["dir"]})
        assert zoomed["root"] == row["dir"] and zoomed["unit_count"] == row["units"]


def test_files_beside_subdirectories_get_their_own_zoomable_row() -> None:
    tools = CartogateTools(_mixed_store())

    overview = dispatch(tools, "repo_map", {"module": "app"})
    direct = [row for row in overview["dirs"] if row["dir"] == "app/."]
    zoomed = dispatch(tools, "repo_map", {"module": "app/."})

    assert direct == [{"dir": "app/.", "units": 1, "symbols": 1}]
    assert [u["unit"] for u in zoomed["units"]] == ["app/main.py"]


def test_a_flat_repository_pages_its_files() -> None:
    store = InMemoryStore()
    store.bulk_load(
        (
            f"mod_{n:03d}.py",
            [symbol(f"mod_{n:03d}.run", f"mod_{n:03d}.py", signature="def run():")],
            [],
        )
        for n in range(300)
    )
    tools = CartogateTools(store)

    first = dispatch(tools, "repo_map", {})
    second = dispatch(tools, "repo_map", {"offset": first["next_offset"]})

    assert first["unit_count"] == 300 and first["truncated"] is True and "dirs" not in first
    assert [u["unit"] for u in first["units"]][0] == "mod_000.py"
    assert second["units"][0]["unit"] == f"mod_{first['next_offset']:03d}.py"
    assert compact_size(first) <= RESPONSE_CHAR_BUDGET


def test_repo_map_of_twenty_thousand_files_fits() -> None:
    tools = CartogateTools(wide_store(20_000))

    overview = dispatch(tools, "repo_map", {})
    zoomed = dispatch(tools, "repo_map", {"module": "svc"})

    assert overview["unit_count"] == 20_000 and compact_size(overview) <= RESPONSE_CHAR_BUDGET
    assert zoomed["dir_count"] == 200 and zoomed["truncated"] is True
    assert compact_size(zoomed) <= RESPONSE_CHAR_BUDGET


def test_a_file_with_hundreds_of_exports_is_paged() -> None:
    store = InMemoryStore()
    exports = [
        symbol(f"big.fn_{n:03d}", "big.py", line=n + 1, signature=f"def fn_{n:03d}(x):")
        for n in range(300)
    ]
    store.bulk_load([("big.py", exports, [])])

    result = dispatch(CartogateTools(store), "repo_map", {"module": "big.py"})

    assert result["found"] is True and result["count"] == 300
    assert result["returned"] == len(result["exports"]) <= DEFAULT_LIMIT
    assert result["truncated"] is True
    assert result["exports"][0] == {
        "qualified_name": "big.fn_000",
        "kind": "symbol",
        "location": "big.py:1",
        "signature": "def fn_000(x):",
    }
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"module": "svc/area_03"},
        {"module": "svc/area_03/pkg_04"},
        {"module": "svc/area_03/pkg_04/file_000.py"},
        {"module": "zz/missing.py"},
        {"limit": 200},
        {"module": "svc/area_03", "offset": 4},
    ],
)
def test_repo_map_shapes_validate_against_the_advertised_schema(
    wide_tools: CartogateTools, args: dict[str, Any]
) -> None:
    result = dispatch(wide_tools, "repo_map", args)
    schema = _SPECS["repo_map"]["output_schema"]

    jsonschema.validate(result, schema)
    jsonschema.validate(result, _with_operational_shapes(schema))
    assert compact_size(result) <= RESPONSE_CHAR_CEILING


# --- findings from the Task 4 review ---------------------------------------------------------


def _store_of(units: list[str]) -> InMemoryStore:
    """A store with one exported function per unit path."""
    store = InMemoryStore()
    store.bulk_load(
        (unit, [symbol(f"m{index}.run", unit, signature="def run():")], [])
        for index, unit in enumerate(units)
    )
    return store


def _walk_dirs(tools: CartogateTools, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``dirs`` row of an overview, following ``next_offset`` to the end."""
    rows: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        result = dispatch(tools, "repo_map", {**args, "offset": offset})
        rows.extend(result["dirs"])
        offset = result.get("next_offset")
    return rows


def test_dir_rows_partition_the_repository(wide_tools: CartogateTools) -> None:
    overview = dispatch(wide_tools, "repo_map", {})

    assert sum(row["units"] for row in overview["dirs"]) == overview["unit_count"]
    assert sum(row["symbols"] for row in overview["dirs"]) == overview["node_count"]


def test_paged_dir_rows_still_partition_their_directory() -> None:
    tools = CartogateTools(wide_store(20_000))

    rows = _walk_dirs(tools, {"module": "svc"})

    assert len(rows) == 200 and len({row["dir"] for row in rows}) == 200
    assert sum(row["units"] for row in rows) == 20_000


def test_a_directory_is_not_confused_with_a_sibling_sharing_its_prefix() -> None:
    units = [f"app/f{n:02d}.py" for n in range(15)] + [f"app2/g{n:02d}.py" for n in range(15)]
    tools = CartogateTools(_store_of(units))

    overview = dispatch(tools, "repo_map", {})
    zoomed = dispatch(tools, "repo_map", {"module": "app"})

    assert overview["dirs"] == [
        {"dir": "app", "units": 15, "symbols": 15},
        {"dir": "app2", "units": 15, "symbols": 15},
    ]
    assert zoomed["unit_count"] == 15
    assert all(row["unit"].startswith("app/") for row in zoomed["units"])


def test_root_level_files_get_a_dot_row_that_zooms() -> None:
    units = ["setup.py", "conftest.py"] + [f"pkg_{n:02d}/mod.py" for n in range(30)]
    tools = CartogateTools(_store_of(units))

    rows = _walk_dirs(tools, {})
    zoomed = dispatch(tools, "repo_map", {"module": "."})

    assert rows[0] == {"dir": ".", "units": 2, "symbols": 2}
    assert [row["unit"] for row in zoomed["units"]] == ["conftest.py", "setup.py"]
    assert zoomed["root"] == "." and zoomed["unit_count"] == 2


def test_every_dir_row_round_trips_on_irregular_layouts() -> None:
    """A seeded sweep over uneven trees: for each, every directory row an overview emits must
    select, as ``module``, exactly the units it counted, and the rows must cover every unit."""
    rng = random.Random(20261115)
    names = ["a", "ab", "app", "app2", "src", "src.v", "x y", "lib"]
    for _ in range(40):
        units = sorted(
            {
                "/".join([*(rng.choice(names) for _ in range(rng.randint(0, 4))), f"f{n}.py"])
                for n in range(rng.randint(25, 120))
            }
        )
        tools = CartogateTools(_store_of(units))
        for limit in (None, 5, 60):
            args = {} if limit is None else {"limit": limit}
            first = dispatch(tools, "repo_map", args)
            if "dirs" not in first:
                continue
            rows = _walk_dirs(tools, args)
            assert sum(row["units"] for row in rows) == len(units)
            for row in rows:
                zoomed = dispatch(tools, "repo_map", {"module": row["dir"]})
                assert zoomed["unit_count"] == row["units"], (row, units)


def test_dependents_are_capped_flagged_and_counted() -> None:
    target = symbol("hub.shared", "hub.py", signature="def shared():")
    loaded: list[tuple[str, list[Any], list[Any]]] = [("hub.py", [target], [])]
    for n in range(400):
        unit = f"clients/client_{n:03d}.py"
        importer = symbol(f"clients.client_{n:03d}.use", unit, signature="def use():")
        edge = Edge(
            type=EdgeType.IMPORTS,
            src=importer.id,
            dst=target.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
        )
        loaded.append((unit, [importer], [edge]))
    store = InMemoryStore()
    store.bulk_load(loaded)
    tools = CartogateTools(store)

    result = dispatch(tools, "repo_map", {"module": "hub.py"})

    shown = len(result["dependents"])
    assert len(tools.repo_map("hub.py")["dependents"]) == 400
    assert result["dependent_count"] == 400 and result["dependents_truncated"] is True
    assert result["dependents"] == [f"clients/client_{n:03d}.py" for n in range(shown)]
    assert 1 <= shown < 400
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    jsonschema.validate(result, _SPECS["repo_map"]["output_schema"])


def test_a_file_with_one_export_gives_most_of_the_room_to_its_dependents() -> None:
    """A widely imported file with a small API: the dependents list should use the room the
    exports do not need, not stop at a fixed fraction."""
    target = symbol("hub.shared", "hub.py", signature="def shared():")
    loaded: list[tuple[str, list[Any], list[Any]]] = [("hub.py", [target], [])]
    for n in range(400):
        unit = f"clients/client_{n:03d}.py"
        importer = symbol(f"clients.client_{n:03d}.use", unit, signature="def use():")
        edge = Edge(
            type=EdgeType.IMPORTS,
            src=importer.id,
            dst=target.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
        )
        loaded.append((unit, [importer], [edge]))
    store = InMemoryStore()
    store.bulk_load(loaded)

    result = dispatch(CartogateTools(store), "repo_map", {"module": "hub.py", "limit": 200})

    assert len(result["dependents"]) == 200 and result["dependents_truncated"] is True


def test_a_file_with_few_dependents_lists_them_all_unflagged(wide_tools: CartogateTools) -> None:
    result = dispatch(wide_tools, "repo_map", {"module": "svc/area_03/pkg_04/file_000.py"})

    assert result["dependents"] == [] and result["dependent_count"] == 0
    assert "dependents_truncated" not in result


def test_an_export_without_a_signature_omits_the_key() -> None:
    node = Node.create(
        repo_id="budget",
        qualified_name="consts.LIMIT",
        kind=NodeKind.SYMBOL,
        name="LIMIT",
        unit="consts.py",
        signature=None,
        location=Location(path="consts.py", start_line=1, end_line=1),
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash="x",
        is_top_level=True,
    )
    store = InMemoryStore()
    store.bulk_load([("consts.py", [node], [])])

    result = dispatch(CartogateTools(store), "repo_map", {"module": "consts.py"})

    assert result["exports"] == [
        {"qualified_name": "consts.LIMIT", "kind": "symbol", "location": "consts.py:1"}
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"module": "."},
        {"module": "svc/."},
        {"module": "svc", "limit": "junk"},
        {"offset": 10**6},
        {"module": "svc/area_03/pkg_04/file_000.py", "offset": 10**6},
    ],
)
def test_odd_repo_map_calls_validate_and_fit(
    wide_tools: CartogateTools, args: dict[str, Any]
) -> None:
    result = dispatch(wide_tools, "repo_map", args)
    schema = _SPECS["repo_map"]["output_schema"]

    jsonschema.validate(result, schema)
    jsonschema.validate(result, _with_operational_shapes(schema))
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_the_repo_map_description_says_how_to_zoom_and_what_dot_means() -> None:
    description = _SPECS["repo_map"]["description"]

    assert "module=" in description and "`x/.`" in description
    assert "dependency direction" not in description  # only a zoomed-in file carries dependents


# --- many changed symbols behind one test (Codex review of #218) ----------------------------


def _fan_in_tools(symbols: int = 900) -> tuple[CartogateTools, list[str]]:
    """``symbols`` functions that are all called by one test function."""
    functions = [
        symbol(f"pkg.part_{n:03d}.fn", f"pkg/part_{n:03d}.py", signature="def fn():")
        for n in range(symbols)
    ]
    test = symbol(
        "tests.test_all.test_everything", "tests/test_all.py", signature="def test_everything():"
    )
    calls = [
        Edge(
            type=EdgeType.CALLS,
            src=test.id,
            dst=function.id,
            provenance=Provenance.TREE_SITTER,
            confidence=Confidence.EXTRACTED,
            source_location=SourceLocation(path="tests/test_all.py", line=10 + index),
        )
        for index, function in enumerate(functions)
    ]
    store = InMemoryStore()
    store.bulk_load(
        [(function.unit, [function], []) for function in functions]
        + [("tests/test_all.py", [test], calls)]
    )
    return CartogateTools(store), [function.qualified_name for function in functions]


@pytest.mark.parametrize("name", ["impact_summary", "suggest_tests"])
@pytest.mark.parametrize(
    ("paging", "ceiling"), [({}, RESPONSE_CHAR_BUDGET), ({"limit": 200}, RESPONSE_CHAR_CEILING)]
)
def test_a_change_to_hundreds_of_symbols_behind_one_test_fits(
    name: str, paging: dict[str, int], ceiling: int
) -> None:
    tools, names = _fan_in_tools()
    raw = getattr(tools, name)(symbols=names)
    assert len(raw["tests"]) == 1 and len(raw["tests"][0]["exercises"]) == 900  # the full API

    result = dispatch(tools, name, {"symbols": names, **paging})

    assert compact_size(result) <= ceiling
    assert result["tests"][0]["more_exercises"] == 900 - ROW_LIST_CAP
    jsonschema.validate(result, _SPECS[name]["output_schema"])


@pytest.mark.parametrize("name", ["impact_summary", "suggest_tests", "doc_drift"])
def test_a_huge_symbol_name_is_not_echoed_back(hub_tools: CartogateTools, name: str) -> None:
    result = dispatch(hub_tools, name, {"symbols": [HUB, "y" * 50_000]})

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert result["unresolved_symbols"] == {} and result["unresolved_count"] == 1
    jsonschema.validate(result, _SPECS[name]["output_schema"])
