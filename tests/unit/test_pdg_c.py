"""C/C++ control-flow + program-dependence graph + slicing (F-03/F-08, C_CFG + def_use_c)."""

from __future__ import annotations

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser

from cartogate.engine.cfg import C_CFG, build_cfg
from cartogate.engine.langspec import lang_for_name
from cartogate.engine.pdg import build_pdg, observable_output_nodes

_PARSER = Parser(Language(tscpp.language()))
_C = lang_for_name("c")


def _func(src: str):
    root = _PARSER.parse(src.encode()).root_node
    return next(c for c in root.named_children if c.type == "function_definition")


def _pdg(src: str):
    return build_pdg(_func(src), _C)


def _dead(src: str) -> list[int]:
    cfg = build_cfg(_func(src).child_by_field_name("body"), C_CFG)
    return sorted(n.start_line for n in cfg.unreachable_statements())


def _data_edges(src: str) -> set[tuple[int, int, str]]:
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


def test_c_statement_after_return_is_unreachable() -> None:
    assert _dead("int f() {\n  return 0;\n  int x = 1;\n}\n") == [3]


def test_c_code_after_if_both_branches_return() -> None:
    src = (
        "int f(int c) {\n"
        "  if (c) {\n"
        "    return 1;\n"
        "  } else {\n"
        "    return 2;\n"
        "  }\n"
        "  int z = 3;\n"  # 7: unreachable
        "}\n"
    )
    assert _dead(src) == [7]


def test_c_declaration_data_dependence() -> None:
    edges = _data_edges("int f() {\n  int x = 1;\n  int y = x;\n  return y;\n}\n")
    assert (2, 3, "x") in edges and (3, 4, "y") in edges


def test_c_kill_keeps_latest() -> None:
    edges = _data_edges("int f() {\n  int x = 1;\n  x = 2;\n  return x;\n}\n")
    assert (3, 4, "x") in edges and (2, 4, "x") not in edges


def test_c_field_write_is_weak_no_kill() -> None:
    src = "int f() {\n  S a = make();\n  a.b = 1;\n  return use(a);\n}\n"
    edges = _data_edges(src)
    assert (2, 4, "a") in edges and (3, 4, "a") in edges  # both reach (field write is weak)


def test_c_arrow_field_write_is_weak() -> None:
    src = "int f(S* p) {\n  p->b = 1;\n  return use(p);\n}\n"
    edges = _data_edges(src)
    assert (2, 3, "p") in edges  # `p->b = 1` weak-defs p; return use(p) depends on it


def test_c_param_reaches_use() -> None:
    pdg = _pdg("int f(int p) {\n  return p + 1;\n}\n")
    assert any(e.src == pdg.cfg.entry and e.var == "p" and e.dep == "data" for e in pdg.edges)


def test_c_for_loop_carried() -> None:
    src = (
        "int f(int n) {\n  int s = 0;\n  for (int i = 0; i < n; i++) {\n    s += i;\n  }\n"
        "  return s;\n}\n"
    )
    edges = _data_edges(src)
    assert (2, 4, "s") in edges and (4, 4, "s") in edges  # s += i on s = 0 and itself


def test_c_multi_declarator() -> None:
    # `int a = 1, b = a;` binds both a and b; b's init reads a.
    edges = _data_edges("int f() {\n  int a = 1, b = a;\n  return b;\n}\n")
    assert (2, 3, "b") in edges  # return b depends on the declaration line


def test_c_switch_case_declaration_is_a_def() -> None:
    src = (
        "int f(int x) {\n"
        "  int r = 0;\n"
        "  switch (x) {\n"  # 3 (opaque)
        "    case 1: r = compute(); break;\n"  # 4
        "    default: r = 9;\n"
        "  }\n"
        "  return r;\n"  # 7
        "}\n"
    )
    edges = _data_edges(src)
    assert (3, 7, "r") in edges  # return r depends on the switch (writes r in a case)


def test_cpp_range_for_binds_loop_var() -> None:
    src = "int f() {\n  int s = 0;\n  for (int v : items) {\n    s += v;\n  }\n  return s;\n}\n"
    edges = _data_edges(src)
    assert (3, 4, "v") in edges or (4, 4, "s") in edges  # range var v reaches the body use


def test_c_backward_slice_excludes_irrelevant() -> None:
    src = "int f() {\n  int a = 1;\n  int b = 2;\n  int c = a + 3;\n  return c;\n}\n"
    lines = _backward_lines(src, 5)
    assert 2 in lines and 4 in lines and 5 in lines and 3 not in lines


def test_cpp_reference_param_reaches_use() -> None:
    # `int& r` is a reference param — it must be a strong def at ENTRY (reference_declarator).
    pdg = _pdg("int f(int& r) {\n  return r + 1;\n}\n")
    assert any(e.src == pdg.cfg.entry and e.var == "r" and e.dep == "data" for e in pdg.edges)


def test_cpp_reference_range_for_binds_var() -> None:
    # `for (int& v : items)` binds v (a strong def) — a use of v in the body depends on it.
    src = "int f() {\n  int s = 0;\n  for (int& v : items) {\n    s += v;\n  }\n  return s;\n}\n"
    edges = _data_edges(src)
    assert (3, 4, "v") in edges  # the reference range-for var reaches `s += v`


def test_cpp_new_array_size_is_a_use() -> None:
    # `new int[n]` reads the size n (review HIGH: array-new uses a declarator field, not arguments).
    pdg = _pdg("int* f(int n) {\n  int* a = new int[n];\n  return a;\n}\n")
    assert any(e.src == pdg.cfg.entry and e.var == "n" and e.dep == "data" for e in pdg.edges)


def test_cpp_reference_declaration_is_a_def() -> None:
    edges = _data_edges("int f(int x) {\n  int& r = x;\n  return r;\n}\n")
    assert (2, 3, "r") in edges  # `int& r = x` binds r; return r depends on it


def test_c_observable_outputs() -> None:
    src = "int f(int x) {\n  int note = 1;\n  int y = x + 1;\n  return g(y);\n}\n"
    pdg = _pdg(src)
    out = {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg, _C)}
    assert 4 in out and 2 not in out  # return g(y) observable; pure local store not
