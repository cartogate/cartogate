"""Program-dependence graph + slicing (F-03)."""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from cartogate.engine.pdg import build_pdg, observable_output_nodes

_PARSER = Parser(Language(tspython.language()))


def _pdg(src: str):
    root = _PARSER.parse(src.encode()).root_node
    func = next(c for c in root.named_children if c.type == "function_definition")
    return build_pdg(func)


def _data_edges(src: str) -> set[tuple[int, int, str]]:
    """{(src_line, dst_line, var)} for data-dependence edges."""
    pdg = _pdg(src)
    return {
        (pdg.cfg.nodes[e.src].start_line, pdg.cfg.nodes[e.dst].start_line, e.var)
        for e in pdg.edges
        if e.dep == "data"
    }


def _backward_lines(src: str, seed_line: int) -> list[int]:
    pdg = _pdg(src)
    seed = pdg.seed_for_line(seed_line)
    assert seed is not None
    return pdg.slice_lines(pdg.backward_slice([seed]))


def test_simple_data_dependence() -> None:
    edges = _data_edges("def f():\n    x = 1\n    y = x\n    return y\n")
    assert (2, 3, "x") in edges  # y = x depends on x = 1
    assert (3, 4, "y") in edges  # return y depends on y = x


def test_kill_keeps_only_latest_def() -> None:
    edges = _data_edges("def f():\n    x = 1\n    x = 2\n    y = x\n")
    assert (3, 4, "x") in edges  # y = x depends on x = 2 (the live def)
    assert (2, 4, "x") not in edges  # the killed x = 1 does NOT reach


def test_weak_def_does_not_kill() -> None:
    # a = f(); a.b = 1; y = a -> y depends on BOTH (the attribute write is a weak def, no kill)
    edges = _data_edges("def f():\n    a = make()\n    a.b = 1\n    y = a\n")
    assert (2, 4, "a") in edges and (3, 4, "a") in edges


def test_parameter_reaches_use() -> None:
    pdg = _pdg("def f(p):\n    return p + 1\n")
    entry = pdg.cfg.entry
    # the `return` (line 2) has a data edge from ENTRY (the parameter def)
    assert any(e.src == entry and e.var == "p" and e.dep == "data" for e in pdg.edges)


def test_loop_carried_dependence() -> None:
    src = "def f(xs):\n    s = 0\n    for i in xs:\n        s += i\n    return s\n"
    edges = _data_edges(src)
    assert (2, 4, "s") in edges  # s += i depends on s = 0
    assert (4, 4, "s") in edges  # ... and on itself (loop-carried, via the back-edge)


def test_backward_slice_excludes_irrelevant() -> None:
    src = "def f():\n    a = 1\n    b = 2\n    c = a + 3\n    return c\n"
    lines = _backward_lines(src, seed_line=5)  # slice from `return c`
    assert 2 in lines and 4 in lines and 5 in lines  # a=1, c=a+3, return c
    assert 3 not in lines  # b = 2 is irrelevant to c


def test_forward_slice() -> None:
    src = "def f():\n    a = 1\n    b = 2\n    c = a + 3\n    return c\n"
    pdg = _pdg(src)
    seed = pdg.seed_for_line(2)  # `a = 1`
    affected = pdg.slice_lines(pdg.forward_slice([seed]))
    assert 4 in affected and 5 in affected  # c = a+3, return c
    assert 3 not in affected  # b = 2 is not affected by a


def test_backward_slice_includes_control() -> None:
    src = "def f(k):\n    if k:\n        c = 1\n    else:\n        c = 2\n    return c\n"
    lines = _backward_lines(src, seed_line=6)  # slice from `return c`
    assert 2 in lines  # the condition (control) is in the slice
    assert 3 in lines and 5 in lines  # both assignments to c


def test_deterministic() -> None:
    src = "def f():\n    x = 1\n    y = x\n    return y\n"
    assert _pdg(src).edges == _pdg(src).edges


def _output_lines(src: str) -> set[int]:
    pdg = _pdg(src)
    return {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg)}


def test_observable_outputs_return_and_call_not_dead_store() -> None:
    src = "def f(x):\n    note = 'hi'\n    y = x + 1\n    return g(y)\n"
    out = _output_lines(src)
    assert 4 in out  # `return g(y)` — a return AND a call
    assert 2 not in out and 3 not in out  # pure local stores are not observable


def test_observable_outputs_external_write() -> None:
    src = "def f(o):\n    o.attr = 1\n    return 0\n"
    out = _output_lines(src)
    assert 2 in out  # `o.attr = 1` mutates an external object (a weak def) -> observable
    assert 3 in out  # `return 0` is observable


def test_observable_outputs_ignores_nested_def_body() -> None:
    # A nested def that is only bound (never called) is not an observable output, even though its
    # body contains a call -> the binding statement must not be a seed.
    src = "def f():\n    def inner():\n        return side()\n    return 1\n"
    out = _output_lines(src)
    assert 4 in out  # `return 1`
    assert 2 not in out  # binding `inner` (its body doesn't run here) is not observable


def test_observable_outputs_global_and_nonlocal_writes() -> None:
    # A plain-name write to a `global` is an external write -> observable (review HIGH regression).
    src = "def f(x):\n    global done\n    done = x\n    return 1\n"
    out = _output_lines(src)
    assert 3 in out  # `done = x` mutates a module global, even though it is a strong def
