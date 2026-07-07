"""Intraprocedural CFG (F-03) — construction + statement-level unreachable detection."""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from cartogate.engine.cfg import build_cfg

_LANG = Language(tspython.language())
_PARSER = Parser(_LANG)


def _cfg(src: str):
    """Build the CFG for the first top-level function in ``src``."""
    root = _PARSER.parse(src.encode("utf-8")).root_node
    func = next(c for c in root.named_children if c.type == "function_definition")
    return build_cfg(func.child_by_field_name("body"))


def _dead_lines(src: str) -> list[int]:
    return sorted(n.start_line for n in _cfg(src).unreachable_statements())


def test_linear_body_is_fully_reachable() -> None:
    src = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
    assert _dead_lines(src) == []  # nothing dead


def test_statement_after_return_is_unreachable() -> None:
    src = "def f():\n    return 1\n    x = 2\n"  # x = 2 (line 3) can never run
    assert _dead_lines(src) == [3]


def test_code_after_if_where_both_branches_return() -> None:
    # The CFG catches what a naive "after return" scan misses: both branches leave, so the
    # trailing statement is unreachable even though no single `return` precedes it.
    src = "def f(c):\n    if c:\n        return 1\n    else:\n        return 2\n    x = 3\n"
    assert _dead_lines(src) == [6]


def test_if_with_fallthrough_branch_keeps_trailing_reachable() -> None:
    # One branch falls through (no else / a non-returning branch), so the trailing code is live.
    src = "def f(c):\n    if c:\n        return 1\n    x = 2\n    return x\n"
    assert _dead_lines(src) == []


def test_loop_body_and_after_are_reachable() -> None:
    src = (
        "def f(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        if i:\n            continue\n"
        "        total += i\n"
        "    return total\n"
    )
    assert _dead_lines(src) == []  # break/continue/loop modelled -> nothing wrongly dead


def test_dead_loop_reports_once_not_per_body_line() -> None:
    # A wholly-unreachable loop is one finding (the loop statement), not the header + each body
    # line — the header spans the full statement so the dead body dedups under it (while == for).
    while_src = "def f():\n    return 1\n    while True:\n        x = 2\n        y = 3\n"
    for_src = "def f():\n    return 1\n    for i in r:\n        x = 2\n        y = 3\n"
    assert _dead_lines(while_src) == [3]
    assert _dead_lines(for_src) == [3]


def test_unmodelled_try_falls_through_conservatively() -> None:
    # `try` is not modelled explicitly; it must be treated as fall-through so the code after it is
    # NOT wrongly flagged dead (soundness: no false positives).
    src = (
        "def f():\n"
        "    try:\n        x = risky()\n    except Exception:\n        x = 0\n"
        "    return x\n"
    )
    assert _dead_lines(src) == []


def test_cfg_has_entry_exit_and_edges() -> None:
    cfg = _cfg("def f():\n    return 1\n")
    assert cfg.entry in cfg.nodes and cfg.exit in cfg.nodes
    assert cfg.nodes[cfg.entry].kind == "entry"
    assert cfg.exit in cfg.reachable_from_entry()  # the return reaches EXIT
