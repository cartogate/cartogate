"""Token budget for the MCP surface: the limits, argument clamping, and a deterministic pager.

Every MCP tool result is paid for in the calling agent's context, and a result that is too large
is not even shown (Claude Code diverts text over 50,000 characters to a file). This leaf module
holds the numbers the surface is held to and the one routine that cuts a row list to fit them.
It imports nothing heavy, so the presentation step can use it on every call.

Sizes are measured as the separator-free JSON serialisation, which is the string a structured
host passes to the model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Rows returned when the caller passes no ``limit``.
DEFAULT_LIMIT = 20
#: The largest ``limit`` honoured.
MAX_LIMIT = 200
#: Ceiling, in characters, for a call that passes no ``limit`` (about 2,000 tokens).
RESPONSE_CHAR_BUDGET = 8_000
#: Ceiling, in characters, for any call, whatever ``limit`` it asks for.
RESPONSE_CHAR_CEILING = 40_000
#: Call-site line numbers shown per reference row.
SITE_LINES_CAP = 5
#: Items shown from a list nested in a row (a test's ``exercises``, a doc's ``mentions``). Such
#: a list grows with the size of the change, not with the row, so uncapped it makes one row as
#: large as the whole result.
ROW_LIST_CAP = 5
#: File paths listed in a truncated result's rollup.
UNITS_CAP = 40
#: Bytes of name + description + inputSchema across the lazy server's tools: the part every
#: host sends to the model. Like the total below it is measured with ``json.dumps`` defaults
#: (the SDK's compact wire form is about 7% smaller) and is a ratchet, set just above the
#: measured size (11,047): adding to the listing means raising it on purpose.
LISTING_VISIBLE_BUDGET = 11_500
#: Bytes of the lazy server's whole ``tools/list`` payload (measured: 30,475).
LISTING_TOTAL_BUDGET = 31_000


def compact_size(payload: object) -> int:
    """Characters in the separator-free JSON serialisation of ``payload``.

    ``payload`` must be JSON-ready (the plain dicts, lists, strings and numbers tool results are
    made of). Non-ASCII text is counted in its ``\\uXXXX`` escaped form, which is how the text
    channel sends it and is never shorter than the unescaped form, so the budget errs small.
    """
    return len(json.dumps(payload, separators=(",", ":")))


def _as_int(value: object) -> int | None:
    """``value`` as an integer, or ``None`` when it is absent, a boolean, or not a number.

    A boolean is rejected explicitly: ``int(True)`` is 1, and a caller that sent ``true`` for a
    row count did not mean "one row". Otherwise anything ``int()`` accepts is accepted: a float
    is truncated and a numeric string (``"7"``, ``" 7 "``) is parsed, because clients differ in
    how they encode a number and an optional knob is not worth failing a call over.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):  # "junk", nan, inf
        return None


def clamp_limit(value: object) -> int | None:
    """A caller's ``limit`` within ``[1, MAX_LIMIT]``, or ``None`` when absent or junk.

    ``None`` is meaningful to :func:`page`: the caller did not choose a size, so the result is
    held to the smaller default budget.
    """
    number = _as_int(value)
    if number is None:
        return None
    return max(1, min(MAX_LIMIT, number))


def clamp_offset(value: object) -> int:
    """A caller's ``offset`` as a non-negative integer; 0 when absent, junk, or negative."""
    number = _as_int(value)
    return max(0, number) if number is not None else 0


def ceiling_for(limit: int | None) -> int:
    """The character ceiling of a call, given its clamped ``limit``.

    A caller that chose no limit gets the small default budget; one that asked for a size has
    accepted a larger answer, up to the hard ceiling.
    """
    return RESPONSE_CHAR_BUDGET if limit is None else RESPONSE_CHAR_CEILING


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a row list: the rows kept, the total, and where the next page starts."""

    rows: list[Any]
    total: int
    #: Offset of the first row not returned; ``None`` when no row follows this page.
    next_offset: int | None


def page(
    rows: Sequence[Any],
    *,
    limit: int | None,
    offset: int,
    reserve: int = 0,
    keep_one: bool = True,
) -> Page:
    """Cut ``rows`` to one page that fits the character ceiling.

    Takes up to ``limit`` rows from ``offset`` (``DEFAULT_LIMIT`` when ``limit`` is ``None``), then
    keeps the longest prefix whose serialisation fits. The ceiling is :func:`ceiling_for` the
    limit, less ``reserve`` (the characters the rest of the result needs). At least one row is
    kept when any is in range, so a single oversized row is returned rather than an empty page
    that hides a match. A result made of several lists that share one ceiling passes
    ``keep_one=False``: there an oversized row is left out (the page is empty and
    ``next_offset`` points at it), because one such row per list would break the ceiling.

    A row is any JSON-ready value (see :func:`compact_size`): a dict, a list of names, a string.
    Callers pass ``limit`` and ``offset`` through :func:`clamp_limit` and :func:`clamp_offset`;
    out-of-range integers are still clamped here, so no value of the three numbers can raise or
    lift the ceiling.
    """
    total = len(rows)
    start = max(0, offset)
    count = DEFAULT_LIMIT if limit is None else max(1, min(MAX_LIMIT, limit))
    # A negative reserve would raise the ceiling it exists to lower.
    ceiling = ceiling_for(limit) - max(0, reserve)

    kept: list[Any] = []
    used = 2  # the enclosing brackets of the serialised list
    for row in rows[start : start + count]:
        cost = compact_size(row) + (1 if kept else 0)  # plus the separating comma
        if (kept or not keep_one) and used + cost > ceiling:
            break
        kept.append(row)
        used += cost

    end = start + len(kept)
    return Page(rows=kept, total=total, next_offset=end if end < total else None)


def envelope(result: Page) -> dict[str, Any]:
    """The paging keys a tool result carries: the true total, how many came back, and what's next.

    ``count`` is always the total number of matches, never the number returned — an agent sizing
    a change by its dependents must not be told a truncated number.
    """
    out: dict[str, Any] = {
        "count": result.total,
        "returned": len(result.rows),
        "truncated": result.next_offset is not None,
    }
    if result.next_offset is not None:
        out["next_offset"] = result.next_offset
    return out


def unit_rollup(
    units: Iterable[str], *, max_chars: int = RESPONSE_CHAR_BUDGET // 4
) -> dict[str, Any]:
    """The distinct file paths behind a row list, sorted, with the total.

    Attached to a result that does not show every match, so cutting rows never hides how many
    files are involved. The list itself is capped at ``UNITS_CAP`` paths and at ``max_chars``
    characters (a quarter of the default budget, so long paths cannot crowd out the rows); at
    least one path is kept. ``unit_count`` is always the true number of distinct files.
    """
    distinct = sorted(set(units))
    kept: list[str] = []
    used = 2  # the enclosing brackets of the serialised list
    for path in distinct[:UNITS_CAP]:
        cost = compact_size(path) + (1 if kept else 0)  # plus the separating comma
        if kept and used + cost > max_chars:
            break
        kept.append(path)
        used += cost
    return {"units": kept, "unit_count": len(distinct)}
