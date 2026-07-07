"""Post-dominators + control dependence (F-03)."""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from cartogate.engine.cfg import build_cfg
from cartogate.engine.postdom import control_dependences, post_dominators

_PARSER = Parser(Language(tspython.language()))


def _cfg(src: str):
    root = _PARSER.parse(src.encode()).root_node
    func = next(c for c in root.named_children if c.type == "function_definition")
    return build_cfg(func.child_by_field_name("body"))


def _ctrl_dep_lines(src: str) -> set[tuple[int, int]]:
    """{(predicate_line, dependent_line)} — lines, for readable assertions."""
    cfg = _cfg(src)
    return {
        (cfg.nodes[c.predicate].start_line, cfg.nodes[c.dependent].start_line)
        for c in control_dependences(cfg)
    }


def test_straight_line_has_no_control_deps() -> None:
    assert _ctrl_dep_lines("def f():\n    a = 1\n    b = 2\n    return b\n") == set()


def test_if_body_is_control_dependent_on_condition() -> None:
    # line 2 = if cond ; line 3 = x = 1 (guarded) ; line 4 = y = 2 (unconditional)
    deps = _ctrl_dep_lines("def f(c):\n    if c:\n        x = 1\n    y = 2\n    return y\n")
    assert (2, 3) in deps  # x = 1 depends on the condition
    assert not any(dep == 4 for _, dep in deps)  # y = 2 is not control-dependent


def test_both_branches_depend_on_condition() -> None:
    deps = _ctrl_dep_lines(
        "def f(c):\n    if c:\n        a = 1\n    else:\n        a = 2\n    return a\n"
    )
    assert (2, 3) in deps and (2, 5) in deps  # both arms depend on the condition


def test_nested_if() -> None:
    src = "def f(c, d):\n    if c:\n        if d:\n            x = 1\n    return 0\n"
    deps = _ctrl_dep_lines(src)
    assert (2, 3) in deps  # inner-if depends on outer condition
    assert (3, 4) in deps  # x = 1 depends on inner condition


def test_loop_body_depends_on_header_post_loop_does_not() -> None:
    src = (
        "def f(items):\n"
        "    total = 0\n"
        "    for i in items:\n"
        "        total += i\n"
        "    return total\n"
    )
    deps = _ctrl_dep_lines(src)
    assert (3, 4) in deps  # `total += i` (line 4) depends on the loop header (line 3)
    assert not any(dep == 5 for _, dep in deps)  # `return total` is not loop-dependent


def test_loop_with_break_topology() -> None:
    src = (
        "def f(xs):\n"
        "    for x in xs:\n"  # 2 = loop header
        "        if x:\n"  # 3
        "            break\n"  # 4
        "        y = x\n"  # 5
        "    return 0\n"  # 6
    )
    cfg = _cfg(src)
    deps = control_dependences(cfg)
    lines = {(cfg.nodes[c.predicate].start_line, cfg.nodes[c.dependent].start_line) for c in deps}
    assert (2, 3) in lines  # the inner `if` runs only while the loop iterates
    assert (3, 4) in lines  # `break` depends on the inner condition
    # `y = x` runs only when the break did NOT fire -> depends on the inner condition (not header)
    assert (3, 5) in lines


def test_simple_loop_header_is_self_dependent() -> None:
    cfg = _cfg("def f(xs):\n    for x in xs:\n        use(x)\n    return 0\n")
    deps = control_dependences(cfg)
    assert any(  # the header decides whether another iteration runs
        c.predicate == c.dependent and cfg.nodes[c.predicate].start_line == 2 for c in deps
    )


def test_post_dominators_exit_reachable_and_self_inclusive() -> None:
    cfg = _cfg("def f():\n    return 1\n")
    pdoms = post_dominators(cfg)
    assert cfg.exit in pdoms and pdoms[cfg.exit] == {cfg.exit}
    for node, doms in pdoms.items():
        assert node in doms  # every node post-dominates itself
        assert cfg.exit in doms  # EXIT post-dominates everything


def test_deterministic() -> None:
    src = "def f(c):\n    if c:\n        a = 1\n    else:\n        a = 2\n    return a\n"
    assert control_dependences(_cfg(src)) == control_dependences(_cfg(src))
