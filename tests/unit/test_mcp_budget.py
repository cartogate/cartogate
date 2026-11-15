"""The MCP response budget: limits, argument clamping, and the deterministic pager.

These are the numbers every MCP tool result is held to (see ``cartogate.mcp.budget``): a call that
passes no ``limit`` fits ``RESPONSE_CHAR_BUDGET``; any call fits ``RESPONSE_CHAR_CEILING``.
"""

from __future__ import annotations

import pytest

from cartogate.mcp.budget import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RESPONSE_CHAR_BUDGET,
    RESPONSE_CHAR_CEILING,
    UNITS_CAP,
    Page,
    ceiling_for,
    clamp_limit,
    clamp_offset,
    compact_size,
    envelope,
    page,
    unit_rollup,
)


def test_compact_size_counts_the_separator_free_serialisation() -> None:
    assert compact_size({"a": [1, 2], "b": "x"}) == len('{"a":[1,2],"b":"x"}')


def test_page_defaults_to_the_default_limit() -> None:
    rows = [{"n": i} for i in range(300)]

    result = page(rows, limit=None, offset=0)

    assert len(result.rows) == DEFAULT_LIMIT and result.total == 300
    assert result.next_offset == DEFAULT_LIMIT


def test_page_trims_rows_to_the_char_budget() -> None:
    rows = [{"text": "x" * 1_000} for _ in range(50)]

    result = page(rows, limit=None, offset=0)

    assert compact_size(result.rows) <= RESPONSE_CHAR_BUDGET
    assert 1 <= len(result.rows) < DEFAULT_LIMIT and result.next_offset == len(result.rows)


def test_page_keeps_one_oversized_row() -> None:
    result = page([{"text": "x" * 100_000}], limit=None, offset=0)

    assert len(result.rows) == 1 and result.next_offset is None


def test_page_past_the_end_is_empty_and_not_truncated() -> None:
    result = page([{"n": 1}], limit=None, offset=5)

    assert result.rows == [] and result.total == 1 and result.next_offset is None


def test_explicit_limit_uses_the_hard_ceiling() -> None:
    rows = [{"text": "x" * 1_000} for _ in range(300)]

    result = page(rows, limit=MAX_LIMIT, offset=0)

    assert compact_size(result.rows) <= RESPONSE_CHAR_CEILING and len(result.rows) > DEFAULT_LIMIT


def test_page_reserve_leaves_room_for_the_rest_of_the_result() -> None:
    rows = [{"text": "x" * 500} for _ in range(50)]

    result = page(rows, limit=None, offset=0, reserve=6_000)

    assert compact_size(result.rows) <= RESPONSE_CHAR_BUDGET - 6_000
    assert len(result.rows) >= 1


def test_page_continues_from_an_offset() -> None:
    rows = [{"n": i} for i in range(45)]

    result = page(rows, limit=None, offset=40)

    assert [row["n"] for row in result.rows] == [40, 41, 42, 43, 44]
    assert result.total == 45 and result.next_offset is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("7", 7),
        (0, 1),
        (-3, 1),
        (10**9, MAX_LIMIT),
        ("junk", None),
        (True, None),
        (2.9, 2),
    ],
)
def test_clamp_limit(raw: object, expected: int | None) -> None:
    assert clamp_limit(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 0), (-1, 0), ("4", 4), ("junk", 0), (True, 0)]
)
def test_clamp_offset(raw: object, expected: int) -> None:
    assert clamp_offset(raw) == expected


def test_envelope_reports_the_total_and_how_to_continue() -> None:
    assert envelope(Page(rows=[{}], total=3, next_offset=1)) == {
        "count": 3,
        "returned": 1,
        "truncated": True,
        "next_offset": 1,
    }


def test_envelope_of_a_complete_result_has_no_next_offset() -> None:
    assert envelope(Page(rows=[{}], total=1, next_offset=None)) == {
        "count": 1,
        "returned": 1,
        "truncated": False,
    }


def test_unit_rollup_is_sorted_distinct_and_capped() -> None:
    rolled = unit_rollup(f"pkg/m{i:03d}.py" for i in list(range(60)) * 2)

    assert rolled["unit_count"] == 60 and len(rolled["units"]) == UNITS_CAP
    assert rolled["units"] == sorted(rolled["units"])
    assert rolled["units"][0] == "pkg/m000.py"


def _sized_row(chars: int) -> dict[str, str]:
    """A row whose compact serialisation is exactly ``chars`` characters."""
    row = {"t": "x" * (chars - 8)}
    assert compact_size(row) == chars
    return row


def test_page_keeps_rows_that_fill_the_ceiling_exactly() -> None:
    rows = [_sized_row(1_998), _sized_row(1_998), _sized_row(1_998), _sized_row(2_001)]
    assert compact_size(rows) == RESPONSE_CHAR_BUDGET

    result = page(rows, limit=None, offset=0)

    assert len(result.rows) == 4 and result.next_offset is None


def test_page_drops_the_row_that_crosses_the_ceiling_by_one() -> None:
    rows = [_sized_row(1_998), _sized_row(1_998), _sized_row(1_998), _sized_row(2_002)]
    assert compact_size(rows) == RESPONSE_CHAR_BUDGET + 1

    result = page(rows, limit=None, offset=0)

    assert len(result.rows) == 3 and result.next_offset == 3


def test_the_hard_ceiling_trims_a_full_explicit_page() -> None:
    rows = [_sized_row(1_000) for _ in range(MAX_LIMIT)]

    result = page(rows, limit=MAX_LIMIT, offset=0)

    assert len(result.rows) == 39  # 2 + 39 * 1_000 + 38 commas = 39_040; a 40th row is 40_041
    assert compact_size(result.rows) <= RESPONSE_CHAR_CEILING and result.next_offset == 39


def test_an_explicit_small_limit_returns_exactly_that_many() -> None:
    result = page([{"n": i} for i in range(50)], limit=5, offset=10)

    assert [row["n"] for row in result.rows] == [10, 11, 12, 13, 14] and result.next_offset == 15


def test_a_negative_offset_starts_at_the_first_row() -> None:
    result = page([{"n": i} for i in range(3)], limit=None, offset=-7)

    assert [row["n"] for row in result.rows] == [0, 1, 2]


@pytest.mark.parametrize("reserve", [-(10**9), -1])
def test_a_negative_reserve_never_raises_the_ceiling(reserve: int) -> None:
    rows = [_sized_row(300) for _ in range(300)]

    result = page(rows, limit=MAX_LIMIT, offset=0, reserve=reserve)

    assert compact_size(result.rows) <= RESPONSE_CHAR_CEILING


@pytest.mark.parametrize("reserve", [RESPONSE_CHAR_BUDGET, RESPONSE_CHAR_BUDGET + 1, 10**9])
def test_a_reserve_that_leaves_no_room_still_returns_one_row(reserve: int) -> None:
    result = page([{"n": i} for i in range(10)], limit=None, offset=0, reserve=reserve)

    assert [row["n"] for row in result.rows] == [0] and result.next_offset == 1


@pytest.mark.parametrize("limit", [None, -5, 0, 1, 10**100])
@pytest.mark.parametrize("offset", [-(10**100), -1, 0, 7, 10**100])
@pytest.mark.parametrize("reserve", [-(10**100), 0, 10**100])
def test_page_never_raises_and_is_deterministic(
    limit: int | None, offset: int, reserve: int
) -> None:
    rows = [{"n": i} for i in range(30)]

    first = page(rows, limit=limit, offset=offset, reserve=reserve)
    second = page(rows, limit=limit, offset=offset, reserve=reserve)

    assert first == second and first.total == 30
    assert len(first.rows) <= MAX_LIMIT
    assert (first.rows != []) == (max(0, offset) < 30)


@pytest.mark.parametrize(
    "junk",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        [],
        {},
        [3],
        {"limit": 3},
        b"7",
        pytest.param("9" * 5_000, id="5000-digit-string"),
        "1e5",
    ],
)
def test_clamps_treat_non_numbers_as_absent(junk: object) -> None:
    assert clamp_limit(junk) is None
    assert clamp_offset(junk) == 0


@pytest.mark.parametrize(
    ("raw", "limit", "offset"),
    [
        # An explicit id: pytest builds ids with str(), which refuses a 5,000-digit integer.
        pytest.param(10**5_000, MAX_LIMIT, 10**5_000, id="5000-digit-int"),
        pytest.param(1e300, MAX_LIMIT, int(1e300), id="1e300"),
        pytest.param(3.7, 3, 3, id="float"),
        pytest.param(" 7 ", 7, 7, id="padded-string"),
    ],
)
def test_clamps_accept_anything_int_accepts(raw: object, limit: int, offset: int) -> None:
    assert clamp_limit(raw) == limit
    assert clamp_offset(raw) == offset


def test_envelope_of_an_empty_result() -> None:
    assert envelope(Page(rows=[], total=0, next_offset=None)) == {
        "count": 0,
        "returned": 0,
        "truncated": False,
    }


def test_a_later_page_that_is_still_truncated_points_past_itself() -> None:
    result = page([{"n": i} for i in range(100)], limit=None, offset=DEFAULT_LIMIT)

    assert result.next_offset == 2 * DEFAULT_LIMIT
    assert envelope(result)["next_offset"] == 2 * DEFAULT_LIMIT


def test_unit_rollup_of_nothing_and_of_a_few() -> None:
    assert unit_rollup(iter(())) == {"units": [], "unit_count": 0}
    assert unit_rollup(iter(["b.py", "a.py", "b.py"])) == {
        "units": ["a.py", "b.py"],
        "unit_count": 2,
    }


def test_unit_rollup_is_cut_to_a_quarter_of_the_budget() -> None:
    rolled = unit_rollup(f"{'deep/' * 40}file_{i:03d}.py" for i in range(100))

    assert rolled["unit_count"] == 100
    assert 1 <= len(rolled["units"]) < UNITS_CAP
    assert compact_size(rolled["units"]) <= RESPONSE_CHAR_BUDGET // 4


def test_unit_rollup_keeps_one_path_however_long() -> None:
    path = "x" * 10_000 + ".py"

    assert unit_rollup([path]) == {"units": [path], "unit_count": 1}


def test_ceiling_for_a_call_depends_on_whether_it_chose_a_limit() -> None:
    assert ceiling_for(None) == RESPONSE_CHAR_BUDGET
    assert ceiling_for(1) == RESPONSE_CHAR_CEILING
    assert ceiling_for(MAX_LIMIT) == RESPONSE_CHAR_CEILING


def test_page_cuts_a_list_of_plain_strings_too() -> None:
    names = [f"pkg.mod_{i:04d}.name" for i in range(2_000)]

    result = page(names, limit=None, offset=0, reserve=RESPONSE_CHAR_BUDGET - 200)

    assert result.rows == names[: len(result.rows)] and 1 <= len(result.rows) < DEFAULT_LIMIT
    assert compact_size(result.rows) <= 200 and result.total == 2_000


def test_page_can_return_nothing_when_told_not_to_keep_an_oversized_row() -> None:
    """A result made of several lists cannot let each keep one oversized row: together they
    would break the ceiling the lists share. Such a caller asks for a strict fit."""
    rows = [{"text": "x" * 5_000}, {"text": "y"}]

    strict = page(rows, limit=None, offset=0, reserve=RESPONSE_CHAR_BUDGET - 100, keep_one=False)
    lenient = page(rows, limit=None, offset=0, reserve=RESPONSE_CHAR_BUDGET - 100)

    assert strict.rows == [] and strict.total == 2 and strict.next_offset == 0
    assert len(lenient.rows) == 1
