"""The MCP presentation step: what ``dispatch`` does to a tool's full result before it is sent.

``present`` works on plain dicts, so these tests feed it hand-built results in the shape the
``CartogateTools`` methods return. The end-to-end budget checks live in
``tests/integration/test_mcp_surface_budget.py``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from cartogate.mcp.budget import (
    DEFAULT_LIMIT,
    RESPONSE_CHAR_BUDGET,
    RESPONSE_CHAR_CEILING,
    ROW_LIST_CAP,
    SITE_LINES_CAP,
    UNITS_CAP,
    compact_size,
)
from cartogate.mcp.present import present


def _ref(
    qname: str,
    path: str,
    line: int,
    sites: list[str],
    signature: str | None = "def f():",
) -> dict[str, Any]:
    """One ``find_references`` row as ``CartogateTools.find_references`` returns it."""
    return {
        "qualified_name": qname,
        "kind": "symbol",
        "unit": path,
        "location": f"{path}:{line}",
        "signature": signature,
        "sites": sites,
    }


def _references(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"found": True, "qualified_name": "p.hub", "references": rows, "count": len(rows)}


def test_reference_rows_drop_unit_and_keep_site_lines() -> None:
    raw = _references([_ref("p.a.f", "p/a.py", 3, ["p/a.py:9", "p/a.py:4"])])

    row = present("find_references", raw, {})["references"][0]

    assert row == {
        "qualified_name": "p.a.f",
        "kind": "symbol",
        "location": "p/a.py:3",
        "lines": [4, 9],
    }


def test_site_lines_are_capped_with_a_remainder() -> None:
    sites = [f"p/a.py:{n}" for n in range(1, 13)]

    row = present("find_references", _references([_ref("p.a.f", "p/a.py", 1, sites)]), {})[
        "references"
    ][0]

    assert row["lines"] == [1, 2, 3, 4, 5] and row["more_lines"] == 7
    assert len(row["lines"]) == SITE_LINES_CAP


def test_signature_survives_only_for_overloads() -> None:
    refs = [
        _ref("p.a.f", "p/a.py", 1, [], "def f(x):"),
        _ref("p.a.f", "p/a.py", 9, [], "def f(x, y):"),
        _ref("p.a.g", "p/a.py", 20, []),
    ]

    rows = present("find_references", _references(refs), {})["references"]

    assert [r.get("signature") for r in rows] == ["def f(x):", "def f(x, y):", None]


def test_a_site_in_another_file_is_kept_verbatim() -> None:
    raw = _references([_ref("p.a.f", "p/a.py", 1, ["p/b.py:7"])])

    row = present("find_references", raw, {})["references"][0]

    assert row["sites"] == ["p/b.py:7"] and row["lines"] == []


def test_one_huge_reference_row_is_returned_with_capped_lines() -> None:
    sites = [f"p/a.py:{n}" for n in range(1, 5_001)]

    result = present("find_references", _references([_ref("p.a.f", "p/a.py", 1, sites)]), {})

    assert result["returned"] == 1 and result["truncated"] is False
    assert result["references"][0]["more_lines"] == 5_000 - SITE_LINES_CAP
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_miss_results_pass_through() -> None:
    raw = {"found": False, "qualified_name": "zz", "candidates": [], "references": [], "count": 0}

    assert present("find_references", raw, {}) == {**raw, "returned": 0, "truncated": False}


def test_unknown_tool_is_unchanged() -> None:
    assert present("find_symbol", {"found": True, "id": "x"}, {}) == {"found": True, "id": "x"}


def test_present_does_not_mutate_the_raw_result() -> None:
    raw = _references([_ref(f"p.m{i}.f", f"p/m{i}.py", 1, [f"p/m{i}.py:2"]) for i in range(50)])
    before = copy.deepcopy(raw)

    present("find_references", raw, {})

    assert raw == before


def test_a_truncated_result_names_every_file_and_a_complete_one_does_not() -> None:
    many = _references([_ref(f"p.m{i:03d}.f", f"p/m{i:03d}.py", 1, []) for i in range(300)])
    few = _references([_ref(f"p.m{i:03d}.f", f"p/m{i:03d}.py", 1, []) for i in range(3)])

    truncated = present("find_references", many, {})
    complete = present("find_references", few, {})

    assert truncated["count"] == 300 and truncated["returned"] == DEFAULT_LIMIT
    assert truncated["truncated"] is True and truncated["next_offset"] == DEFAULT_LIMIT
    assert truncated["unit_count"] == 300 and len(truncated["units"]) == UNITS_CAP
    assert compact_size(truncated) <= RESPONSE_CHAR_BUDGET
    assert complete["returned"] == 3 and complete["truncated"] is False
    assert "units" not in complete and "next_offset" not in complete


def test_affected_rows_drop_unit_and_signature() -> None:
    raw = {
        "found": True,
        "symbol": "p.hub",
        "depth": 1,
        "affected": [
            {
                "qualified_name": "p.a.f",
                "kind": "symbol",
                "unit": "p/a.py",
                "location": "p/a.py:3",
                "signature": "def f():",
            }
        ],
        "count": 1,
    }

    result = present("blast_radius", raw, {})

    assert result["affected"] == [
        {"qualified_name": "p.a.f", "kind": "symbol", "location": "p/a.py:3"}
    ]
    assert result["depth"] == 1 and result["count"] == 1 and result["returned"] == 1


def test_implementation_rows_take_the_same_shape() -> None:
    raw = {
        "found": True,
        "qualified_name": "p.Base",
        "implementations": [
            {
                "qualified_name": "p.a.Impl",
                "kind": "symbol",
                "unit": "p/a.py",
                "location": "p/a.py:3",
                "signature": "class Impl(Base):",
            }
        ],
        "count": 1,
    }

    result = present("implementations", raw, {})

    assert result["implementations"] == [
        {"qualified_name": "p.a.Impl", "kind": "symbol", "location": "p/a.py:3"}
    ]


def test_sites_in_other_files_are_capped_counted_and_named_in_the_rollup() -> None:
    """A module row of a shared-module language (a C module spanning ``.c`` and ``.h``, a Go
    package) has call sites in files other than its own. Cutting them must not hide a file."""
    sites = ["p/a.c:4", *[f"p/part_{n:02d}.c:{n}" for n in range(30)]]
    raw = _references([_ref("p.a", "p/a.c", 1, sites)])

    result = present("find_references", raw, {})

    row = result["references"][0]
    assert row["lines"] == [4]
    assert row["sites"] == [f"p/part_{n:02d}.c:{n}" for n in range(SITE_LINES_CAP)]
    assert row["more_sites"] == 30 - SITE_LINES_CAP
    assert result["truncated"] is False
    assert result["unit_count"] == 31 and "p/part_29.c" in result["units"]


def test_the_rollup_of_a_truncated_result_counts_site_files_too() -> None:
    rows = [_ref(f"p.m{i:03d}", f"p/m{i:03d}.c", 1, [f"p/m{i:03d}.h:2"]) for i in range(300)]

    result = present("find_references", _references(rows), {})

    assert result["truncated"] is True and result["unit_count"] == 600


def test_a_complete_result_whose_sites_all_show_has_no_rollup() -> None:
    raw = _references([_ref("p.a", "p/a.c", 1, ["p/a.h:2", "p/b.h:3"])])

    result = present("find_references", raw, {})

    assert result["references"][0]["sites"] == ["p/a.h:2", "p/b.h:3"]
    assert "units" not in result and "more_sites" not in result["references"][0]


def test_a_site_line_in_non_ascii_digits_is_not_parsed() -> None:
    raw = _references([_ref("p.a.f", "p/a.py", 1, ["p/a.py:٣"])])  # ARABIC-INDIC DIGIT THREE

    row = present("find_references", raw, {})["references"][0]

    assert row["lines"] == [] and row["sites"] == ["p/a.py:٣"]


def test_long_paths_do_not_push_a_default_result_over_the_budget() -> None:
    deep = "very/long/directory/name/" * 9
    rows = [_ref(f"p.m{i:03d}.f", f"{deep}m{i:03d}.py", 1, []) for i in range(100)]

    result = present("find_references", _references(rows), {})

    assert result["returned"] >= 1 and result["unit_count"] == 100
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_presenting_twice_gives_byte_identical_json() -> None:
    raw = _references(
        [_ref(f"p.m{i:03d}.f", f"p/m{i:03d}.py", 1, [f"p/m{i:03d}.py:9"]) for i in range(50)]
    )

    assert json.dumps(present("find_references", raw, {})) == json.dumps(
        present("find_references", raw, {})
    )


def test_present_leaves_unsorted_nested_site_lists_alone() -> None:
    raw = _references([_ref("p.a.f", "p/a.py", 1, ["p/a.py:9", "p/b.py:7", "p/a.py:2"])])
    before = copy.deepcopy(raw)

    present("find_references", raw, {})

    assert raw == before


def test_implementations_page_to_the_budget() -> None:
    rows = [
        {
            "qualified_name": f"p.m{i:03d}.Impl",
            "kind": "symbol",
            "unit": f"p/m{i:03d}.py",
            "location": f"p/m{i:03d}.py:3",
            "signature": "class Impl(Base):",
        }
        for i in range(300)
    ]
    raw = {"found": True, "qualified_name": "p.Base", "implementations": rows, "count": 300}

    result = present("implementations", raw, {})

    assert result["count"] == 300 and result["returned"] == DEFAULT_LIMIT
    assert result["truncated"] is True and result["unit_count"] == 300
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


# --- paging for the other list-returning tools ----------------------------------------------


def _names(count: int) -> list[str]:
    return [f"pkg.mod_{i:03d}.symbol_{i:03d}" for i in range(count)]


def _assert_first_page(result: dict[str, Any], key: str, full: list[Any]) -> None:
    """The first default page: the head of ``full`` in order, the true total, and the budget."""
    assert result[key] == full[:DEFAULT_LIMIT]
    assert result["count"] == len(full) and result["returned"] == DEFAULT_LIMIT
    assert result["truncated"] is True and result["next_offset"] == DEFAULT_LIMIT
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_suggest_tests_pages_and_names_every_test_file() -> None:
    tests = [
        {
            "qualified_name": f"tests.test_m{i:03d}.test_it",
            "unit": f"tests/test_m{i:03d}.py",
            "exercises": ["p.hub"],
        }
        for i in range(300)
    ]
    raw = {"changed": ["p.hub"], "tests": tests, "count": 300}

    result = present("suggest_tests", raw, {})

    _assert_first_page(result, "tests", tests)
    assert result["unit_count"] == 300 and len(result["units"]) == UNITS_CAP
    assert result["units"][0] == "tests/test_m000.py"


def test_doc_drift_pages_and_names_every_doc_file() -> None:
    docs = [
        {"path": f"docs/page_{i:03d}.md", "unit": f"docs/page_{i:03d}.md", "mentions": ["p.hub"]}
        for i in range(300)
    ]
    raw = {"changed": ["p.hub"], "docs": docs, "count": 300}

    result = present("doc_drift", raw, {})

    _assert_first_page(result, "docs", docs)
    assert result["unit_count"] == 300


def test_find_dead_code_pages_its_candidates() -> None:
    candidates = [
        {"qualified_name": name, "location": f"p/m{i}.py:1"} for i, name in enumerate(_names(300))
    ]

    result = present("find_dead_code", {"candidates": candidates, "count": 300}, {})

    _assert_first_page(result, "candidates", candidates)
    assert "units" not in result


def test_find_duplicate_bodies_pages_its_groups() -> None:
    groups = [[f"p.a.f{i}", f"p.b.f{i}"] for i in range(300)]

    result = present("find_duplicate_bodies", {"groups": groups, "count": 300}, {})

    _assert_first_page(result, "groups", groups)


def test_localize_pages_suspects_in_ranking_order() -> None:
    suspects = [
        {
            "qualified_name": name,
            "location": f"p/m{i}.py:1",
            "distance": 1 + i // 50,
            "changed_lines": [1],
            "output_relevant_lines": [],
            "output_analyzed": False,
        }
        for i, name in enumerate(reversed(_names(300)))
    ]
    raw = {
        "found": True,
        "test": "t.test_x",
        "reason": None,
        "depth_searched": 4,
        "suspects": suspects,
        "count": 300,
    }

    result = present("localize", raw, {})

    _assert_first_page(result, "suspects", suspects)  # engine order kept, not re-sorted


def test_an_offset_continues_where_the_last_page_stopped() -> None:
    candidates = [{"qualified_name": name, "location": "p/m.py:1"} for name in _names(45)]
    raw = {"candidates": candidates, "count": 45}

    second = present("find_dead_code", raw, {"offset": 2 * DEFAULT_LIMIT})

    assert second["candidates"] == candidates[2 * DEFAULT_LIMIT :]
    assert second["count"] == 45 and second["returned"] == 5 and second["truncated"] is False


def test_a_changed_list_too_long_to_echo_is_cut_and_counted() -> None:
    changed = _names(2_000)
    raw = {"changed": changed, "tests": [], "count": 0}

    result = present("suggest_tests", raw, {})

    assert result["changed"] == changed[: len(result["changed"])]
    assert len(result["changed"]) < 2_000 and result["changed_count"] == 2_000
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_a_short_changed_list_is_echoed_whole() -> None:
    raw = {"changed": ["p.a", "p.b"], "tests": [], "count": 0}

    result = present("suggest_tests", raw, {})

    assert result["changed"] == ["p.a", "p.b"] and "changed_count" not in result


def _impact(affected: int, tests: int, docs: int, changed: int = 1) -> dict[str, Any]:
    return {
        "changed": _names(changed),
        "affected": [
            {"qualified_name": name, "unit": f"p/m{i}.py", "location": f"p/m{i}.py:1"}
            for i, name in enumerate(_names(affected))
        ],
        "tests": [
            {
                "qualified_name": f"tests.t{i}.test_it",
                "unit": f"tests/t{i}.py",
                "exercises": ["p.hub"],
            }
            for i in range(tests)
        ],
        "docs": [
            {"path": f"docs/d{i}.md", "unit": f"docs/d{i}.md", "mentions": ["p.hub"]}
            for i in range(docs)
        ],
        "counts": {"changed": changed, "affected": affected, "tests": tests, "docs": docs},
    }


def test_impact_summary_cuts_each_list_and_keeps_the_totals() -> None:
    raw = _impact(affected=300, tests=300, docs=300)

    result = present("impact_summary", raw, {})

    assert result["affected"] == raw["affected"][: len(result["affected"])]
    assert 1 <= len(result["affected"]) <= DEFAULT_LIMIT
    assert 1 <= len(result["tests"]) <= DEFAULT_LIMIT and 1 <= len(result["docs"]) <= DEFAULT_LIMIT
    assert result["counts"] == raw["counts"] and result["truncated"] is True
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_impact_summary_of_a_small_change_is_whole() -> None:
    raw = _impact(affected=2, tests=1, docs=0)

    result = present("impact_summary", raw, {})

    assert result == {**raw, "truncated": False}


def test_impact_summary_of_a_huge_diff_still_fits() -> None:
    raw = _impact(affected=300, tests=300, docs=300, changed=2_000)

    result = present("impact_summary", raw, {})

    assert len(result["changed"]) < 2_000 and result["counts"]["changed"] == 2_000
    assert result["truncated"] is True and compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_an_explicit_limit_widens_every_impact_list() -> None:
    raw = _impact(affected=300, tests=300, docs=300)

    result = present("impact_summary", raw, {"limit": 60})

    assert len(result["affected"]) == 60 and len(result["tests"]) == 60
    assert compact_size(result) <= RESPONSE_CHAR_CEILING


# --- findings from the Task 3 review ---------------------------------------------------------


def _unresolved(count: int) -> dict[str, list[str]]:
    return {
        f"missing_{i:04d}": [f"pkg.mod_{k}.missing_{i:04d}" for k in range(5)] for i in range(count)
    }


@pytest.mark.parametrize(
    ("arguments", "ceiling"), [({}, RESPONSE_CHAR_BUDGET), ({"limit": 200}, RESPONSE_CHAR_CEILING)]
)
def test_a_flood_of_unresolved_names_is_cut_and_counted(
    arguments: dict[str, Any], ceiling: int
) -> None:
    raw = {"changed": [], "tests": [], "count": 0, "unresolved_symbols": _unresolved(2_000)}

    result = present("suggest_tests", raw, arguments)

    assert compact_size(result) <= ceiling
    assert 1 <= len(result["unresolved_symbols"]) < 2_000 and result["unresolved_count"] == 2_000
    assert (
        list(result["unresolved_symbols"])
        == list(raw["unresolved_symbols"])[: len(result["unresolved_symbols"])]
    )


def test_a_few_unresolved_names_are_kept_whole() -> None:
    raw = {"changed": [], "docs": [], "count": 0, "unresolved_symbols": _unresolved(3)}

    result = present("doc_drift", raw, {})

    assert (
        result["unresolved_symbols"] == raw["unresolved_symbols"]
        and "unresolved_count" not in result
    )


def test_impact_summary_survives_a_flood_of_unresolved_names() -> None:
    raw = {**_impact(affected=300, tests=300, docs=300), "unresolved_symbols": _unresolved(3_000)}

    result = present("impact_summary", raw, {})

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert result["unresolved_count"] == 3_000 and result["truncated"] is True
    assert len(result["affected"]) >= 1 and result["counts"] == raw["counts"]


def test_impact_summary_gives_an_empty_lists_share_to_the_others() -> None:
    raw = _impact(affected=500, tests=0, docs=0)
    for row in raw["affected"]:
        row["location"] = "a/very/long/path/that/makes/each/row/large/" * 6 + row["location"]

    result = present("impact_summary", raw, {"limit": 200})

    assert RESPONSE_CHAR_CEILING * 3 // 4 < compact_size(result) <= RESPONSE_CHAR_CEILING
    assert len(result["affected"]) > 60 and result["truncated"] is True


@pytest.mark.parametrize(
    "name", ["suggest_tests", "impact_summary", "localize", "find_duplicate_bodies"]
)
def test_the_new_presenters_do_not_mutate_the_raw_result(name: str) -> None:
    raw: dict[str, Any] = {
        **_impact(affected=50, tests=50, docs=50, changed=50),
        "count": 50,
        "unresolved_symbols": _unresolved(400),
        "suspects": [
            {"qualified_name": n, "location": "p.py:1", "distance": 1} for n in _names(50)
        ],
        "groups": [[f"p.a.f{i}", f"p.b.f{i}"] for i in range(50)],
    }
    before = copy.deepcopy(raw)

    present(name, raw, {"limit": 7, "offset": 3})

    assert raw == before


def test_localize_keeps_its_ranking_on_a_later_page() -> None:
    suspects = [
        {"qualified_name": name, "location": "p.py:1", "distance": 1 + i // 10}
        for i, name in enumerate(reversed(_names(45)))
    ]
    raw = {
        "found": True,
        "test": "t.test_x",
        "reason": None,
        "depth_searched": 4,
        "suspects": suspects,
        "count": 45,
    }

    result = present("localize", raw, {"limit": 10, "offset": 30})

    assert result["suspects"] == suspects[30:40] and result["next_offset"] == 40


# --- a row's nested list must not carry the result past its budget (Codex review of #218) ----


def test_a_test_rows_exercises_are_capped_and_counted() -> None:
    names = _names(900)
    raw = {
        "changed": names,
        "tests": [{"qualified_name": "tests.t.test_all", "unit": "tests/t.py", "exercises": names}],
        "count": 1,
    }

    result = present("suggest_tests", raw, {})

    row = result["tests"][0]
    assert row["exercises"] == names[:ROW_LIST_CAP] and row["more_exercises"] == 900 - ROW_LIST_CAP
    assert row["qualified_name"] == "tests.t.test_all" and row["unit"] == "tests/t.py"
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_a_doc_rows_mentions_are_capped_and_counted() -> None:
    names = _names(900)
    raw = {
        "changed": names,
        "docs": [{"path": "docs/api.md", "unit": "docs/api.md", "mentions": names}],
        "count": 1,
    }

    result = present("doc_drift", raw, {})

    row = result["docs"][0]
    assert row["mentions"] == names[:ROW_LIST_CAP] and row["more_mentions"] == 900 - ROW_LIST_CAP
    assert compact_size(result) <= RESPONSE_CHAR_BUDGET


def test_a_short_nested_list_is_left_alone() -> None:
    raw = {
        "changed": ["p.a"],
        "tests": [{"qualified_name": "tests.t.test_a", "unit": "tests/t.py", "exercises": ["p.a"]}],
        "count": 1,
    }

    assert present("suggest_tests", raw, {})["tests"] == raw["tests"]


@pytest.mark.parametrize(
    ("arguments", "ceiling"),
    [({}, RESPONSE_CHAR_BUDGET), ({"limit": 200}, RESPONSE_CHAR_CEILING)],
)
def test_impact_summary_with_one_huge_test_row_and_one_huge_doc_row_fits(
    arguments: dict[str, Any], ceiling: int
) -> None:
    """900 changed symbols all exercised by one test and mentioned by one document: each of
    those rows used to carry all 900 names, and each list kept its one oversized row."""
    names = _names(900)
    raw = {
        "changed": names,
        "affected": [],
        "tests": [{"qualified_name": "tests.t.test_all", "unit": "tests/t.py", "exercises": names}],
        "docs": [{"path": "docs/api.md", "unit": "docs/api.md", "mentions": names}],
        "counts": {"changed": 900, "affected": 0, "tests": 1, "docs": 1},
    }
    before = copy.deepcopy(raw)

    result = present("impact_summary", raw, arguments)

    assert compact_size(result) <= ceiling
    assert result["counts"] == raw["counts"] and result["truncated"] is True
    assert result["tests"][0]["more_exercises"] == 900 - ROW_LIST_CAP
    assert result["docs"][0]["more_mentions"] == 900 - ROW_LIST_CAP
    assert raw == before


def test_impact_summary_never_exceeds_the_ceiling_even_with_oversized_rows() -> None:
    """The lists of an impact summary share one ceiling, so none may keep a row that does not
    fit what is left: an empty list with `truncated` and the totals in `counts` is the honest
    answer, not a result several times the budget."""
    huge = "x" * 6_000
    raw = {
        "changed": [huge + "a", huge + "b"],
        "affected": [{"qualified_name": huge, "unit": "p/a.py", "location": "p/a.py:1"}],
        "tests": [{"qualified_name": huge, "unit": "tests/t.py", "exercises": ["p.a"]}],
        "docs": [{"path": huge, "unit": "docs/a.md", "mentions": ["p.a"]}],
        "counts": {"changed": 2, "affected": 1, "tests": 1, "docs": 1},
    }

    result = present("impact_summary", raw, {})

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert result["truncated"] is True and result["counts"] == raw["counts"]


@pytest.mark.parametrize("name", ["impact_summary", "suggest_tests", "doc_drift"])
def test_an_oversized_echoed_name_is_dropped_and_counted(name: str) -> None:
    """A caller that passes a 50,000-character name must not get it echoed back: `changed` and
    `unresolved_symbols` repeat the caller's own input, so an entry that does not fit is left
    out and counted, not kept as the one oversized row."""
    huge = "y" * 50_000
    raw: dict[str, Any] = {
        **_impact(affected=2, tests=1, docs=1),
        "count": 1,
        "changed": [huge],
        "unresolved_symbols": {huge: ["pkg.a.candidate"]},
    }

    result = present(name, raw, {})

    assert compact_size(result) <= RESPONSE_CHAR_BUDGET
    assert result["changed"] == []
    assert result["unresolved_symbols"] == {} and result["unresolved_count"] == 1
    if name == "impact_summary":
        assert result["truncated"] is True and result["counts"] == raw["counts"]
    else:
        assert result["changed_count"] == 1


def test_a_nested_list_exactly_at_the_cap_is_not_marked_as_cut() -> None:
    at_cap = _names(ROW_LIST_CAP)
    over = _names(ROW_LIST_CAP + 1)
    raw = {
        "changed": [],
        "tests": [
            {"qualified_name": "tests.t.test_a", "unit": "tests/t.py", "exercises": at_cap},
            {"qualified_name": "tests.t.test_b", "unit": "tests/t.py", "exercises": over},
        ],
        "count": 2,
    }

    first, second = present("suggest_tests", raw, {})["tests"]

    assert first["exercises"] == at_cap and "more_exercises" not in first
    assert second["exercises"] == over[:ROW_LIST_CAP] and second["more_exercises"] == 1
