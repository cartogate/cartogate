"""Go control-flow + program-dependence graph + slicing (F-03/F-08, GO_CFG + def_use_go)."""

from __future__ import annotations

import tree_sitter_go as tsgo
from tree_sitter import Language, Parser

from cartogate.engine.cfg import GO_CFG, build_cfg
from cartogate.engine.langspec import lang_for_name
from cartogate.engine.pdg import build_pdg, observable_output_nodes

_PARSER = Parser(Language(tsgo.language()))
_GO = lang_for_name("go")


def _func(src: str):
    root = _PARSER.parse(src.encode()).root_node
    return next(c for c in root.named_children if c.type == "function_declaration")


def _pdg(src: str):
    return build_pdg(_func(src), _GO)


def _dead(src: str) -> list[int]:
    cfg = build_cfg(_func(src).child_by_field_name("body"), GO_CFG)
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


# --- CFG / unreachable (blocks wrap statements in a statement_list) --------------------------- #


def test_go_statement_after_return_is_unreachable() -> None:
    assert _dead("func f() int {\n\treturn 0\n\tx := 1\n\treturn x\n}\n") == [3, 4]


def test_go_linear_body_reachable() -> None:
    assert _dead("func f(a int) int {\n\tx := a + 1\n\treturn x\n}\n") == []


def test_go_code_after_if_both_branches_return() -> None:
    src = (
        "func f(c bool) int {\n"
        "\tif c {\n"
        "\t\treturn 1\n"
        "\t} else {\n"
        "\t\treturn 2\n"
        "\t}\n"
        "\tx := 3\n"  # 7: unreachable
        "\treturn x\n"  # 8
        "}\n"
    )
    assert _dead(src) == [7, 8]


# --- def/use + slicing ------------------------------------------------------------------------ #


def test_go_short_var_data_dependence() -> None:
    edges = _data_edges("func f() int {\n\tx := 1\n\ty := x\n\treturn y\n}\n")
    assert (2, 3, "x") in edges and (3, 4, "y") in edges


def test_go_kill_keeps_latest() -> None:
    edges = _data_edges("func f() int {\n\tx := 1\n\tx = 2\n\treturn x\n}\n")
    assert (3, 4, "x") in edges and (2, 4, "x") not in edges


def test_go_selector_write_is_weak_no_kill() -> None:
    src = "func f() T {\n\ta := make()\n\ta.b = 1\n\treturn a\n}\n"
    edges = _data_edges(src)
    assert (2, 4, "a") in edges and (3, 4, "a") in edges  # both reach (selector write is weak)


def test_go_param_reaches_use() -> None:
    pdg = _pdg("func f(p int) int {\n\treturn p + 1\n}\n")
    assert any(e.src == pdg.cfg.entry and e.var == "p" and e.dep == "data" for e in pdg.edges)


def test_go_if_initializer_is_a_def() -> None:
    # `if x := g(); x > 0` — the init statement defines x; the body's use of x must depend on it.
    src = (
        "func f() int {\n"
        "\tif x := g(); x > 0 {\n"  # 2: init x := g()  (+ condition)
        "\t\treturn x\n"  # 3: uses x
        "\t}\n"
        "\treturn 0\n"
        "}\n"
    )
    edges = _data_edges(src)
    assert (2, 3, "x") in edges  # return x depends on the if-initializer x := g()


def test_go_backward_slice_excludes_irrelevant() -> None:
    src = "func f() int {\n\ta := 1\n\tb := 2\n\tc := a + 3\n\treturn c\n}\n"
    lines = _backward_lines(src, seed_line=5)
    assert 2 in lines and 4 in lines and 5 in lines and 3 not in lines


def test_go_range_loop_carried() -> None:
    src = (
        "func f(xs []int) int {\n\ts := 0\n\tfor _, v := range xs {\n\t\ts += v\n\t}\n"
        "\treturn s\n}\n"
    )
    edges = _data_edges(src)
    assert (2, 4, "s") in edges and (4, 4, "s") in edges  # s += v on s := 0 and itself


def test_go_labeled_assignment_is_a_def() -> None:
    # `Label: x = 1` — the assignment's def of x must be collected (review MEDIUM regression).
    src = "func f() int {\n\tx := 0\nLoop:\n\tx = 1\n\t_ = Loop\n\treturn x\n}\n"
    edges = _data_edges(src)
    # the labeled stmt is one CFG node (start line 3); its write to x strong-defs and kills x:=0
    assert (3, 6, "x") in edges and (2, 6, "x") not in edges


def test_go_select_receive_binding_is_a_def() -> None:
    # `case v = <-ch:` binds v (to an outer var) — its def must reach a later use (review MEDIUM).
    src = (
        "func f(ch chan int) int {\n"
        "\tv := 0\n"
        "\tselect {\n"  # 3: opaque node folds the select
        "\tcase v = <-ch:\n"  # 4: v rebound from the channel
        "\t\tuse(v)\n"
        "\t}\n"
        "\treturn v\n"  # 7
        "}\n"
    )
    edges = _data_edges(src)
    assert (3, 7, "v") in edges  # return v depends on the select (which rebinds v in a case)


def test_go_c_style_for_loop_carried() -> None:
    src = (
        "func f(n int) int {\n\ts := 0\n\tfor i := 0; i < n; i++ {\n\t\ts += i\n\t}\n"
        "\treturn s\n}\n"
    )
    edges = _data_edges(src)
    assert (2, 4, "s") in edges and (4, 4, "s") in edges  # s += i on s := 0 and itself


def test_go_observable_outputs() -> None:
    src = "func f(x int) int {\n\tnote := 1\n\ty := x + 1\n\treturn g(y)\n}\n"
    pdg = _pdg(src)
    out = {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg, _GO)}
    assert 4 in out and 2 not in out  # return g(y) observable; pure local store not
