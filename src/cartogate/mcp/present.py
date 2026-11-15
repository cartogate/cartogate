"""Presentation step for MCP tool results: page the rows and say each file path once.

``CartogateTools`` methods return full, unpaged results, because the pre-commit gate and the
task CLI call them from Python and read every row. An agent calling the same tool over MCP pays
for the result in its context, so :func:`present` — applied inside ``dispatch``, the one route
the MCP server and the daemon share — cuts the row list to the budget in
:mod:`cartogate.mcp.budget`, reports the true total, and drops fields that repeat the file path.

Nothing here reads the graph: a presenter is a pure function of the method's result and the
caller's arguments, so the same store and arguments always present the same way.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from cartogate.mcp.budget import (
    DEFAULT_LIMIT,
    ROW_LIST_CAP,
    SITE_LINES_CAP,
    ceiling_for,
    clamp_limit,
    clamp_offset,
    compact_size,
    envelope,
    page,
    unit_rollup,
)

#: The shape of tool results, as a number a warm daemon reports in its health. Bump it whenever
#: a tool result or its advertised output schema changes shape, whether the change is made here
#: or in ``mcp/tools.py`` (``TOOL_SPECS``, the tool methods). The MCP server forwards a daemon's
#: results under its OWN advertised output schemas, and a validating client rejects a mismatch;
#: comparing package versions alone misses an editable install, whose version does not move when
#: the source does. ``test_an_output_schema_change_comes_with_a_surface_version_bump`` fails
#: when the schemas change without a bump.
SURFACE_VERSION = 3

Row = dict[str, Any]
_Presenter = Callable[[dict[str, Any], Mapping[str, Any]], dict[str, Any]]


def _path_of(location: str) -> str:
    """The file path of a ``path:line`` location (the last colon, so a drive letter survives)."""
    return location.rpartition(":")[0]


def _overloaded(rows: Iterable[Row]) -> frozenset[str]:
    """Qualified names that more than one row carries."""
    counts = Counter(str(row.get("qualified_name")) for row in rows)
    return frozenset(name for name, seen in counts.items() if seen > 1)


def _brief(row: Row, overloaded: frozenset[str]) -> Row:
    """A dependent's row without the fields that repeat its path.

    ``unit`` equals the path in ``location``. ``signature`` is kept only where it does work:
    telling apart rows that share a qualified name (overloads).
    """
    out: Row = {
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "location": row["location"],
    }
    if row["qualified_name"] in overloaded and row.get("signature") is not None:
        out["signature"] = row["signature"]
    return out


def _site_paths(row: Row) -> list[str]:
    """The file of every call site on a raw ``find_references`` row."""
    return [_path_of(str(site)) for site in row.get("sites") or ()]


def _reference(row: Row, overloaded: frozenset[str]) -> Row:
    """A ``find_references`` row: the brief plus its call sites.

    A function's call sites are in its own file, so their ``path:line`` strings repeat the
    row's path; the line numbers alone carry the same information (``lines``). A MODULE row of
    a shared-module language is the exception: one node stands for a C module's ``.c`` and
    ``.h``, or for every file of a Go package, so its sites span files. Those keep their
    ``path:line`` form under ``sites``, capped like the lines, with the remainder counted.
    """
    out = _brief(row, overloaded)
    path = _path_of(str(row["location"]))
    lines: list[int] = []
    elsewhere: list[str] = []
    for site in row.get("sites") or ():
        site_path, _, line = str(site).rpartition(":")
        # isascii: str.isdigit() also accepts other scripts' digits, which are not line numbers.
        if site_path == path and line.isascii() and line.isdigit():
            lines.append(int(line))
        else:
            elsewhere.append(str(site))
    lines.sort()
    out["lines"] = lines[:SITE_LINES_CAP]
    if len(lines) > SITE_LINES_CAP:
        out["more_lines"] = len(lines) - SITE_LINES_CAP
    if elsewhere:
        out["sites"] = elsewhere[:SITE_LINES_CAP]
        if len(elsewhere) > SITE_LINES_CAP:
            out["more_sites"] = len(elsewhere) - SITE_LINES_CAP
    return out


def _paged(
    raw: dict[str, Any],
    arguments: Mapping[str, Any],
    *,
    key: str,
    rows: Sequence[Any],
    units: Iterable[str] | None = None,
    always_rollup: bool = False,
) -> dict[str, Any]:
    """``raw`` with its ``key`` list replaced by one page of ``rows``, plus the paging keys.

    ``units`` are the files behind ALL the matches. They are attached as a rollup whenever the
    rows shown do not name every file: the page was cut short, or (``always_rollup``) a row's
    own site list was. The page is sized against what the rest of the result needs, so the
    whole result fits the ceiling, not only its rows.
    """
    rollup = unit_rollup(units) if units is not None else {}
    out = dict(raw)
    out[key] = []
    # Sized as if truncated: every paging key present at its widest, and the rollup attached.
    skeleton = {
        **out,
        "count": len(rows),
        "returned": len(rows),
        "truncated": True,
        "next_offset": len(rows),
        **rollup,
    }
    result = page(
        rows,
        limit=clamp_limit(arguments.get("limit")),
        offset=clamp_offset(arguments.get("offset")),
        reserve=compact_size(skeleton),
    )
    out[key] = result.rows
    out.update(envelope(result))
    if result.next_offset is not None or always_rollup:
        out.update(rollup)
    return out


def _dependents(key: str, shape: Callable[[Row, frozenset[str]], Row]) -> _Presenter:
    """A presenter for a list of dependent rows under ``key``, each reshaped by ``shape``."""

    def _present(raw: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        full: list[Row] = list(raw.get(key) or ())
        overloaded = _overloaded(full)
        rows = [shape(row, overloaded) for row in full]
        return _paged(
            raw,
            arguments,
            key=key,
            rows=rows,
            # Every file a match touches: the row's own, and each of its call sites'.
            units=(
                path
                for row in full
                for path in (_path_of(str(row["location"])), *_site_paths(row))
            ),
            always_rollup=any("more_sites" in row for row in rows),
        )

    return _present


def _cap_echoes(raw: dict[str, Any], limit: int | None, *, changed: bool) -> dict[str, Any]:
    """A copy of ``raw`` with the parts that echo the caller's own input cut to size.

    ``changed`` repeats the symbols the caller named (or its diff touched) and
    ``unresolved_symbols`` the names that did not resolve, so on a large diff either can
    outweigh the answer. Each is cut to a fixed share of the ceiling (a quarter and an eighth),
    and when one is cut its true size is recorded beside it. An entry too large for its share
    is left out rather than kept as the one oversized row: the caller already has it.
    ``changed=False`` leaves that list alone, for a caller that sizes it with other lists.
    """
    out = dict(raw)
    ceiling = ceiling_for(limit)

    if changed:
        names = list(raw.get("changed") or ())
        kept = page(
            names, limit=limit, offset=0, reserve=ceiling - ceiling // 4, keep_one=False
        )
        out["changed"] = kept.rows
        if kept.next_offset is not None:
            out["changed_count"] = len(names)

    unresolved = raw.get("unresolved_symbols")
    if unresolved:
        entries = [[name, candidates] for name, candidates in unresolved.items()]
        kept = page(
            entries, limit=limit, offset=0, reserve=ceiling - ceiling // 8, keep_one=False
        )
        if kept.next_offset is not None:
            out["unresolved_symbols"] = dict(kept.rows)
            out["unresolved_count"] = len(entries)
    return out


def _capped(row: Row, field: str) -> Row:
    """``row`` with the list under ``field`` cut to ``ROW_LIST_CAP`` items and the rest counted.

    A test's ``exercises`` and a doc's ``mentions`` name the changed symbols the row touches, so
    a change to hundreds of symbols behind one test makes that one row hundreds of names long.
    """
    items = row.get(field)
    if not isinstance(items, list) or len(items) <= ROW_LIST_CAP:
        return row
    return {**row, field: items[:ROW_LIST_CAP], f"more_{field}": len(items) - ROW_LIST_CAP}


def _listed(
    key: str,
    *,
    unit_key: str | None = None,
    echoes: bool = False,
    capped_field: str | None = None,
) -> _Presenter:
    """A presenter that pages the list under ``key``.

    ``unit_key`` names the row field holding a file path, for the rollup. ``echoes`` marks a
    result that also echoes the caller's input (``changed``, ``unresolved_symbols``), which is
    cut first so the rows are sized against what is left. ``capped_field`` names a list nested
    in each row that is cut to ``ROW_LIST_CAP`` items; other rows are returned as they are.
    """

    def _present(raw: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        full = list(raw.get(key) or ())
        rows = [_capped(row, capped_field) for row in full] if capped_field else full
        if echoes:
            raw = _cap_echoes(raw, clamp_limit(arguments.get("limit")), changed=True)
        units = (str(row[unit_key]) for row in full) if unit_key is not None else None
        return _paged(raw, arguments, key=key, rows=rows, units=units)

    return _present


#: The lists of an ``impact_summary``, which share one ceiling.
_IMPACT_LISTS = ("changed", "affected", "tests", "docs")


def _impact(raw: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """An ``impact_summary`` with each of its lists cut to ``limit``.

    The report is a digest, so it takes ``limit`` but no ``offset``: ``counts`` keeps the
    totals, and ``suggest_tests`` / ``doc_drift`` (same symbols or diff) and ``blast_radius``
    (per symbol) page through what was cut.

    The lists share the ceiling. They are sized smallest first, each offered an equal split of
    what is still unspent, so a list that needs little (or is empty) leaves its share to the
    larger ones instead of wasting it. The fit is strict: a row that does not fit what is left
    is not kept (``truncated`` says so and ``counts`` has the total), because four lists that
    each kept one oversized row would together exceed the ceiling several times over.
    """
    limit = clamp_limit(arguments.get("limit"))
    out = _cap_echoes(raw, limit, changed=False)
    skeleton = {**out, **{key: [] for key in _IMPACT_LISTS}, "truncated": True}
    spare = max(0, ceiling_for(limit) - compact_size(skeleton))

    lists = {key: list(raw.get(key) or ()) for key in _IMPACT_LISTS}
    lists["tests"] = [_capped(row, "exercises") for row in lists["tests"]]
    lists["docs"] = [_capped(row, "mentions") for row in lists["docs"]]
    cut = "unresolved_count" in out
    # Smallest first; the stable sort keeps _IMPACT_LISTS order between lists of equal size.
    by_need = sorted(_IMPACT_LISTS, key=lambda key: compact_size(lists[key]))
    for position, key in enumerate(by_need):
        share = spare // (len(by_need) - position)
        result = page(
            lists[key],
            limit=limit,
            offset=0,
            reserve=ceiling_for(limit) - share,
            keep_one=False,
        )
        out[key] = result.rows
        # Brackets are already in the skeleton; charge only what the rows themselves take.
        spare -= min(spare, max(0, compact_size(result.rows) - 2))
        cut = cut or result.next_offset is not None
    out["truncated"] = cut
    return out


def _zoom_base(root: object) -> tuple[str, bool]:
    """The directory a ``repo_map`` overview is rooted at, and whether it covers only that
    directory's own files (``dir/.``, or ``.`` at the top) rather than its whole subtree."""
    if not isinstance(root, str):
        return "", False
    if root == ".":
        return "", True
    if root.endswith("/."):
        return root[:-2], True
    return root, False


def _dir_parts(unit: str, base: str) -> list[str]:
    """The directory segments of ``unit`` below ``base`` (which is an ancestor of it)."""
    relative = unit[len(base) + 1 :] if base else unit
    return relative.split("/")[:-1]


def _dir_prefix(parts: Sequence[str], base: str, depth: int) -> str:
    """The directory row a unit falls under when directories are shown ``depth`` levels deep.

    A unit nested at least that deep falls under its ancestor at that level. A shallower one
    sits directly in a directory that deeper rows also belong to, so it gets that directory's
    own-files row, ``dir/.`` — which, like every row name, can be passed back as ``module``.
    """
    if len(parts) >= depth:
        return "/".join(([base] if base else []) + list(parts[:depth]))
    own = "/".join(([base] if base else []) + list(parts))
    return f"{own}/." if own else "."


def _dir_rows(units: Sequence[Row], base: str, room: int) -> tuple[list[Row], int]:
    """File rows collapsed into directory rows, at the deepest level that fits ``room`` rows.

    Returns the rows (sorted by directory) and the depth chosen. Depth 1 is used even when it
    does not fit; the caller pages the rows in that case.
    """
    located = [(unit, _dir_parts(str(unit["unit"]), base)) for unit in units]
    deepest = max(len(parts) for _, parts in located)
    depth = 1
    # A deeper level only ever splits rows, so the first level that overflows ends the search.
    for candidate in range(2, deepest + 1):
        if len({_dir_prefix(parts, base, candidate) for _, parts in located}) > room:
            break
        depth = candidate
    totals: dict[str, list[int]] = {}
    for unit, parts in located:
        total = totals.setdefault(_dir_prefix(parts, base, depth), [0, 0])
        total[0] += 1
        total[1] += int(unit["symbols"])
    rows = [
        {"dir": name, "units": files, "symbols": symbols}
        for name, (files, symbols) in sorted(totals.items())
    ]
    return rows, depth


def _unit_row(unit: Row) -> Row:
    """An overview row for one file, its exports by name only: signatures are a zoom away."""
    out: Row = {
        "unit": unit["unit"],
        "symbols": unit["symbols"],
        "exports": [export["name"] for export in unit["exports"]],
    }
    if "more" in unit:
        out["more"] = unit["more"]
    return out


def _export_row(row: Row) -> Row:
    """One export of a zoomed-in file. ``unit`` and ``name`` repeat the file and the qualified
    name; the signature stays, because the file's API is what zooming in is for."""
    out: Row = {
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "location": row["location"],
    }
    if row.get("signature") is not None:
        out["signature"] = row["signature"]
    return out


#: Characters set aside, when sizing a file's dependents, for the detail result's own keys and
#: paging fields (measured at under 300; rounded up so a long unit path still fits).
_DETAIL_OVERHEAD = 512


def _repo_map(raw: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """A ``repo_map`` result that fits whatever the repository's size.

    An overview lists files while they fit in one page; past that it lists directories, as deep
    as still fits, and the caller zooms with ``module=<dir>``. A flat directory has nothing to
    collapse into, so its files are paged instead. A single file's exports are paged too.
    """
    limit = clamp_limit(arguments.get("limit"))
    room = DEFAULT_LIMIT if limit is None else limit

    if "units" in raw:
        units: list[Row] = list(raw["units"])
        base, own_files_only = _zoom_base(raw.get("root"))
        nested = not own_files_only and any(_dir_parts(str(u["unit"]), base) for u in units)
        if len(units) <= room or not nested:
            out = _paged(raw, arguments, key="units", rows=[_unit_row(u) for u in units])
        else:
            rows, depth = _dir_rows(units, base, room)
            rest = {key: value for key, value in raw.items() if key != "units"}
            out = _paged(
                {**rest, "dirs": [], "dir_count": len(rows), "depth": depth},
                arguments,
                key="dirs",
                rows=rows,
            )
        del out["count"]  # `unit_count` and `dir_count` already say how many there are
        return out

    if raw.get("found"):
        exports = [_export_row(row) for row in raw.get("exports") or ()]
        dependents = list(raw.get("dependents") or ())
        ceiling = ceiling_for(limit)
        # Dependents get the room this page of exports does not need (a widely imported file
        # with a small API should list its importers), and never less than a quarter.
        offset = clamp_offset(arguments.get("offset"))
        wanted = compact_size(exports[offset : offset + room]) + _DETAIL_OVERHEAD
        kept = page(
            dependents,
            limit=limit,
            offset=0,
            reserve=ceiling - max(ceiling // 4, ceiling - wanted),
        )
        detail = {**raw, "dependents": kept.rows, "dependent_count": len(dependents)}
        if kept.next_offset is not None:
            # `offset` pages the exports only, so say plainly that this list is partial.
            detail["dependents_truncated"] = True
        return _paged(detail, arguments, key="exports", rows=exports)

    return raw  # a miss: the candidates are already capped


_PRESENTERS: dict[str, _Presenter] = {
    "find_references": _dependents("references", _reference),
    "blast_radius": _dependents("affected", _brief),
    "implementations": _dependents("implementations", _brief),
    "suggest_tests": _listed("tests", unit_key="unit", echoes=True, capped_field="exercises"),
    "doc_drift": _listed("docs", unit_key="unit", echoes=True, capped_field="mentions"),
    "find_dead_code": _listed("candidates"),
    "find_duplicate_bodies": _listed("groups"),
    "localize": _listed("suspects"),
    "impact_summary": _impact,
    "repo_map": _repo_map,
}


def present(name: str, raw: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The result of tool ``name`` as an MCP caller receives it.

    ``raw`` is what the ``CartogateTools`` method returned and is not modified. A tool with no
    presenter is returned as it is.
    """
    presenter = _PRESENTERS.get(name)
    return raw if presenter is None else presenter(raw, arguments)
