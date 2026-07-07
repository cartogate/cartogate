"""JS/TS program-dependence graph + slicing (F-03/F-08) — def/use via the JS_CFG + def_use_js."""

from __future__ import annotations

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

from cartogate.engine.langspec import lang_for_name
from cartogate.engine.pdg import build_pdg, observable_output_nodes

_PARSER = Parser(Language(tstypescript.language_tsx()))
_JS = lang_for_name("typescript")


def _pdg(src: str):
    root = _PARSER.parse(src.encode()).root_node
    func = next(c for c in root.named_children if c.type == "function_declaration")
    return build_pdg(func, _JS)


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


def test_js_data_dependence_through_declarations() -> None:
    edges = _data_edges(
        "function f() {\n  let x = 1;\n  let y = x;\n  return y;\n}\n"
    )
    assert (2, 3, "x") in edges  # y = x depends on x = 1
    assert (3, 4, "y") in edges  # return y depends on y = x


def test_js_kill_keeps_latest() -> None:
    edges = _data_edges(
        "function f() {\n  let x = 1;\n  x = 2;\n  return x;\n}\n"
    )
    assert (3, 4, "x") in edges and (2, 4, "x") not in edges  # x = 2 reaches; x = 1 is killed


def test_js_member_write_is_weak_no_kill() -> None:
    # a = make(); a.b = 1; return a -> return depends on BOTH (the member write is a weak def)
    edges = _data_edges(
        "function f() {\n  let a = make();\n  a.b = 1;\n  return a;\n}\n"
    )
    assert (2, 4, "a") in edges and (3, 4, "a") in edges


def test_js_param_reaches_use() -> None:
    pdg = _pdg("function f(p) {\n  return p + 1;\n}\n")
    entry = pdg.cfg.entry
    assert any(e.src == entry and e.var == "p" and e.dep == "data" for e in pdg.edges)


def test_js_backward_slice_excludes_irrelevant() -> None:
    src = "function f() {\n  let a = 1;\n  let b = 2;\n  let c = a + 3;\n  return c;\n}\n"
    lines = _backward_lines(src, seed_line=5)  # slice from `return c`
    assert 2 in lines and 4 in lines and 5 in lines  # a, c, return
    assert 3 not in lines  # b is irrelevant


def test_js_backward_slice_includes_control() -> None:
    src = (
        "function f(k) {\n"
        "  let c;\n"
        "  if (k) {\n"
        "    c = 1;\n"
        "  } else {\n"
        "    c = 2;\n"
        "  }\n"
        "  return c;\n"
        "}\n"
    )
    lines = _backward_lines(src, seed_line=8)
    assert 3 in lines  # the condition (control) is in the slice
    assert 4 in lines and 6 in lines  # both assignments to c


def test_js_switch_default_declaration_is_a_def() -> None:
    # A `let` inside `switch (…) { default: … }` must be collected as a strong def (the switch is an
    # opaque CFG node, so its body def/use is folded in) — review LOW regression.
    src = (
        "function f(x) {\n"
        "  let z = 0;\n"  # 2
        "  switch (x) {\n"  # 3  (opaque node folds the whole switch)
        "    default:\n"
        "      z = compute();\n"  # 5  -> z written inside default
        "  }\n"
        "  return z;\n"  # 7
        "}\n"
    )
    edges = _data_edges(src)
    # `return z` depends on the switch (which writes z in its default body), not only on `let z = 0`
    assert (3, 7, "z") in edges


def test_js_observable_outputs() -> None:
    src = "function f(x) {\n  let note = 1;\n  let y = x + 1;\n  return g(y);\n}\n"
    pdg = _pdg(src)
    out = {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg, _JS)}
    assert 4 in out  # return g(y) — a return + a call
    assert 2 not in out  # a pure local store is not observable
