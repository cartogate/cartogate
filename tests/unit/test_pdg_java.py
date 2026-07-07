"""Java control-flow + program-dependence graph + slicing (F-03/F-08, JAVA_CFG + def_use_java)."""

from __future__ import annotations

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

from cartogate.engine.cfg import JAVA_CFG, build_cfg
from cartogate.engine.langspec import lang_for_name
from cartogate.engine.pdg import build_pdg, observable_output_nodes

_PARSER = Parser(Language(tsjava.language()))
_JAVA = lang_for_name("java")


def _method(src: str):
    root = _PARSER.parse(src.encode()).root_node
    # class_declaration -> class_body -> method_declaration
    cls = next(c for c in root.named_children if c.type == "class_declaration")
    body = cls.child_by_field_name("body")
    return next(c for c in body.named_children if c.type == "method_declaration")


def _wrap(method_body: str) -> str:
    return "class C {\n  int f(int a) {\n" + method_body + "\n  }\n}\n"


def _pdg(method_body: str):
    return build_pdg(_method(_wrap(method_body)), _JAVA)


def _dead(method_body: str) -> list[int]:
    cfg = build_cfg(_method(_wrap(method_body)).child_by_field_name("body"), JAVA_CFG)
    return sorted(n.start_line for n in cfg.unreachable_statements())


def _data_edges(method_body: str) -> set[tuple[int, int, str]]:
    pdg = _pdg(method_body)
    return {
        (pdg.cfg.nodes[e.src].start_line, pdg.cfg.nodes[e.dst].start_line, e.var)
        for e in pdg.edges
        if e.dep == "data"
    }


def _backward_lines(method_body: str, seed_line: int) -> list[int]:
    pdg = _pdg(method_body)
    seed = pdg.seed_for_line(seed_line)
    assert seed is not None
    return pdg.slice_lines(pdg.backward_slice([seed]))


# lines: 1=class, 2=method sig, then the body starts at 3.


def test_java_statement_after_return_is_unreachable() -> None:
    assert _dead("    return 0;\n    int x = 1;") == [4]


def test_java_local_var_data_dependence() -> None:
    edges = _data_edges("    int x = a;\n    int y = x;\n    return y;")
    assert (3, 4, "x") in edges and (4, 5, "y") in edges


def test_java_kill_keeps_latest() -> None:
    edges = _data_edges("    int x = 1;\n    x = 2;\n    return x;")
    assert (4, 5, "x") in edges and (3, 5, "x") not in edges


def test_java_field_write_is_weak_no_kill() -> None:
    edges = _data_edges("    Obj o = make();\n    o.b = 1;\n    return o;")
    assert (3, 5, "o") in edges and (4, 5, "o") in edges  # both reach (field write is weak)


def test_java_this_field_is_observable_external_write() -> None:
    pdg = _pdg("    this.field = a;\n    return 0;")
    out = {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg, _JAVA)}
    assert 3 in out  # `this.field = a` mutates the object (a weak def of `this`) -> observable


def test_java_param_reaches_use() -> None:
    pdg = _pdg("    return a + 1;")
    assert any(e.src == pdg.cfg.entry and e.var == "a" and e.dep == "data" for e in pdg.edges)


def test_java_c_style_for_loop_carried() -> None:
    edges = _data_edges(
        "    int s = 0;\n    for (int i = 0; i < a; i++) {\n      s += i;\n    }\n    return s;"
    )
    assert (3, 5, "s") in edges and (5, 5, "s") in edges  # s += i on s = 0 and itself


def test_java_enhanced_for_binds_loop_var() -> None:
    edges = _data_edges(
        "    int s = 0;\n    for (int v : items) {\n      s += v;\n    }\n    return s;"
    )
    # v defined by the enhanced-for header reaches `s += v`
    assert (5, 5, "v") in edges or any(
        src == 4 and dst == 5 and var == "v" for src, dst, var in edges
    )


def test_java_backward_slice_excludes_irrelevant() -> None:
    lines = _backward_lines(
        "    int a2 = 1;\n    int b = 2;\n    int c = a2 + 3;\n    return c;", 6
    )
    assert 3 in lines and 5 in lines and 6 in lines and 4 not in lines


def test_java_switch_case_declaration_is_a_def() -> None:
    # A decl in a switch case body must be collected (the switch is one opaque CFG node).
    body = (
        "    int r = 0;\n"
        "    switch (a) {\n"  # 4 (opaque node folds the switch)
        "      case 1:\n"
        "        r = compute();\n"  # 6: r written in a case body
        "        break;\n"
        "      default:\n"
        "        r = 9;\n"
        "    }\n"
        "    return r;"  # 11
    )
    edges = _data_edges(body)
    assert (4, 11, "r") in edges  # return r depends on the switch (which writes r in a case)


def test_java_catch_binding_is_a_def() -> None:
    # `catch (E e)` binds e; a use of e in the catch body must depend on it — review HIGH.
    body = (
        "    try {\n"
        "      risky();\n"
        "    } catch (Exception e) {\n"  # 5 (the try is one opaque node)
        "      log(e);\n"  # 6: uses e
        "    }\n"
        "    return 0;"
    )
    pdg = _pdg(body)
    # the try is one opaque CFG node; the catch param e must be a def of that node (not just a use)
    try_node = next(nid for nid, n in pdg.cfg.nodes.items() if n.kind == "try_statement")
    assert "e" in pdg.du[try_node].defs  # `e` is bound (strong def), not just read


def test_java_update_on_field_is_weak_def() -> None:
    # `obj.f++` writes obj.f -> a weak def of obj; a later read of obj depends on it.
    edges = _data_edges("    Obj obj = make();\n    obj.f++;\n    return obj;")
    assert (3, 5, "obj") in edges and (4, 5, "obj") in edges  # both reach (++ is a weak write)


def test_java_observable_outputs() -> None:
    pdg = _pdg("    int note = 1;\n    int y = a + 1;\n    return g(y);")
    out = {pdg.cfg.nodes[nid].start_line for nid in observable_output_nodes(pdg, _JAVA)}
    assert 5 in out and 3 not in out  # return g(y) observable; pure local store not
