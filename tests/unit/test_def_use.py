"""Per-statement def/use extraction (F-03) — feeds PDG data-dependence."""

from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from cartogate.engine.def_use import DefUse, def_use_for_node, parameter_defs

_PARSER = Parser(Language(tspython.language()))


def _stmt(src: str):
    """The first statement node in a function body, plus a CFG-style kind hint."""
    root = _PARSER.parse(f"def _f():\n    {src}\n".encode()).root_node
    func = next(c for c in root.named_children if c.type == "function_definition")
    body = func.child_by_field_name("body")
    return body.named_children[0]


def _du(src: str, kind: str | None = None) -> DefUse:
    node = _stmt(src)
    return def_use_for_node(node, kind or node.type)


def test_plain_assignment() -> None:
    du = _du("x = a + b")
    assert du.defs == {"x"} and du.uses == {"a", "b"} and du.weak_defs == set()


def test_augmented_assignment_is_def_and_use() -> None:
    du = _du("x += 1")
    assert du.defs == {"x"} and "x" in du.uses


def test_tuple_unpacking() -> None:
    du = _du("x, y = pair")
    assert du.defs == {"x", "y"} and du.uses == {"pair"}


def test_annotated_assignment_reads_annotation_and_value() -> None:
    du = _du("x: T = make(seed)")
    assert du.defs == {"x"} and {"T", "make", "seed"} <= du.uses


def test_attribute_target_is_weak_def_plus_use() -> None:
    du = _du("a.b = x")
    assert du.weak_defs == {"a"} and du.defs == set() and {"a", "x"} <= du.uses


def test_subscript_target_is_weak_def() -> None:
    du = _du("a[i] = x")
    assert du.weak_defs == {"a"} and {"a", "i", "x"} <= du.uses and du.defs == set()


def test_call_arg_and_receiver_are_weak_defs() -> None:
    du = _du("obj.append(x)")
    assert {"obj", "x"} <= du.weak_defs and {"obj", "x"} <= du.uses


def test_for_header_defines_target_uses_iterable_not_body() -> None:
    node = _stmt("for i in items:\n        total += i")
    du = def_use_for_node(node, "for_statement")
    assert du.defs == {"i"} and du.uses == {"items"}  # body (total, i) NOT folded in


def test_while_condition_uses_only() -> None:
    node = _stmt("while go(n):\n        n -= 1")
    du = def_use_for_node(node, "while_statement")
    assert du.uses == {"go", "n"} and du.defs == set()  # body not folded


def test_if_condition_reads_and_walrus() -> None:
    node = _stmt("if (m := compute(x)):\n        pass")
    cond = node.child_by_field_name("condition")
    du = def_use_for_node(cond, "condition")
    assert du.defs == {"m"} and {"compute", "x"} <= du.uses


def test_with_as_binds_alias_uses_context() -> None:
    node = _stmt("with open(path) as fh:\n        pass")
    du = def_use_for_node(node, "with_statement")
    assert du.defs == {"fh"} and {"open", "path"} <= du.uses


def test_comprehension_inner_names_are_uses() -> None:
    du = _du("result = [f(y) for y in xs]")
    assert du.defs == {"result"} and {"f", "xs"} <= du.uses  # over-approx; y harmless if present


def test_nested_def_is_opaque_capture() -> None:
    node = _stmt("def inner():\n        return q + r")
    du = def_use_for_node(node, "function_definition")
    assert du.defs == {"inner"} and {"q", "r"} <= du.uses


def test_global_is_def_and_use() -> None:
    node = _stmt("global g")
    du = def_use_for_node(node, "global_statement")
    assert du.defs == {"g"} and du.uses == {"g"}


def test_del_is_use_only() -> None:
    node = _stmt("del x")
    du = def_use_for_node(node, "delete_statement")
    assert du.uses == {"x"} and du.defs == set()


def test_parameter_defs() -> None:
    root = _PARSER.parse(b"def f(a, b=1, *c, **d):\n    return a\n").root_node
    func = next(n for n in root.named_children if n.type == "function_definition")
    assert parameter_defs(func) == {"a", "b", "c", "d"}


def test_parameter_defs_typed_and_splat() -> None:
    root = _PARSER.parse(b"def f(a: int, *args: int, **kw: str) -> None:\n    return a\n").root_node
    func = next(n for n in root.named_children if n.type == "function_definition")
    assert parameter_defs(func) == {"a", "args", "kw"}


def test_import_binds_a_def() -> None:
    du = _du("import os")
    assert du.defs == {"os"} and "os" not in du.uses  # bound name is a def, not a use


def test_from_import_and_alias_bind_defs() -> None:
    assert _du("from foo import bar, baz").defs == {"bar", "baz"}
    assert _du("from foo import bar as b").defs == {"b"}
    assert _du("import pkg.sub as p").defs == {"p"}


def test_walrus_in_assignment_rhs_is_a_def() -> None:
    du = _du("x = (y := compute())")
    assert {"x", "y"} <= du.defs and "compute" in du.uses


def test_with_body_defs_are_folded_in() -> None:
    # `with` is opaque in the CFG (one node), so its body's defs must be captured here.
    node = _stmt("with open(p) as fh:\n        data = fh.read()")
    du = def_use_for_node(node, "with_statement")
    assert {"fh", "data"} <= du.defs and {"open", "p", "fh"} <= du.uses


def test_try_body_and_except_as_defs_are_folded_in() -> None:
    # `try` is opaque — body assignments AND `except E as e` must contribute defs (not just uses).
    src = "try:\n        a = risky()\n    except E as e:\n        a = recover(e)"
    node = _stmt(src)
    du = def_use_for_node(node, "try_statement")
    assert {"a", "e"} <= du.defs and {"risky", "recover", "E"} <= du.uses


def test_match_capture_is_a_weak_def_not_lost() -> None:
    # `match` is opaque; capture names bind but are weak (no kill, so a same-named def isn't lost).
    src = "match cmd:\n        case Point(q):\n            z = use(q)"
    node = _stmt(src)
    du = def_use_for_node(node, "match_statement")
    assert "z" in du.defs and "q" in du.weak_defs and {"cmd", "use"} <= du.uses
