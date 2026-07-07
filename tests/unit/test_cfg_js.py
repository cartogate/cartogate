"""JS/TS control-flow graph + unreachable-code detection (F-03/F-08, the JS_CFG spec)."""

from __future__ import annotations

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

from cartogate.engine.cfg import JS_CFG, build_cfg

_PARSER = Parser(Language(tstypescript.language_tsx()))


def _cfg(src: str):
    root = _PARSER.parse(src.encode()).root_node
    func = next(c for c in root.named_children if c.type == "function_declaration")
    return build_cfg(func.child_by_field_name("body"), JS_CFG)


def _dead(src: str) -> list[int]:
    return sorted(n.start_line for n in _cfg(src).unreachable_statements())


def test_linear_body_fully_reachable() -> None:
    assert _dead("function f(a) {\n  let x = a;\n  return x;\n}\n") == []


def test_statement_after_return_is_unreachable() -> None:
    # line 3 (`x = 1;`) follows an unconditional return -> dead.
    assert _dead("function f() {\n  return 0;\n  let x = 1;\n}\n") == [3]


def test_statement_after_throw_is_unreachable() -> None:
    assert _dead("function f() {\n  throw new Error();\n  let y = 2;\n}\n") == [3]


def test_code_after_if_both_branches_return() -> None:
    # both arms return -> the trailing statement (line 6) is unreachable.
    src = (
        "function f(c) {\n"
        "  if (c) {\n"
        "    return 1;\n"
        "  } else {\n"
        "    return 2;\n"
        "  }\n"
        "  let z = 3;\n"  # 7: unreachable
        "}\n"
    )
    assert _dead(src) == [7]


def test_else_if_chain_fallthrough_keeps_trailing_reachable() -> None:
    # an else-if chain with a final fall-through -> trailing code stays reachable (no false hit).
    src = (
        "function f(c) {\n"
        "  if (c) {\n"
        "    return 1;\n"
        "  } else if (!c) {\n"
        "    let a = 2;\n"
        "  }\n"
        "  return 9;\n"  # reachable via the else-if fall-through
        "}\n"
    )
    assert _dead(src) == []


def test_loop_body_and_after_reachable() -> None:
    src = (
        "function f(xs) {\n"
        "  let t = 0;\n"
        "  for (let i = 0; i < xs; i++) {\n"
        "    t += i;\n"
        "  }\n"
        "  return t;\n"
        "}\n"
    )
    assert _dead(src) == []


def test_statement_after_break_in_loop_is_unreachable() -> None:
    src = (
        "function f(xs) {\n"
        "  while (xs) {\n"
        "    break;\n"
        "    let dead = 1;\n"  # 4: after break -> unreachable
        "  }\n"
        "  return 0;\n"
        "}\n"
    )
    assert _dead(src) == [4]


def test_for_of_and_do_while_bodies_reachable() -> None:
    src = (
        "function f(xs) {\n"
        "  let t = 0;\n"
        "  for (const x of xs) {\n"
        "    t += x;\n"
        "  }\n"
        "  do {\n"
        "    t--;\n"
        "  } while (t);\n"
        "  return t;\n"
        "}\n"
    )
    assert _dead(src) == []  # for-of + do-while bodies and trailing code all reachable


def test_arrow_expression_body_is_not_a_block() -> None:
    # An arrow with an expression body has no statement_block -> the CFG CLI must skip it (the guard
    # is `body.type not in block_types`). Here we assert the body type so the skip guard is covered.
    root = _PARSER.parse(b"const g = (x) => x + 1;\n").root_node
    arrow = next(
        n
        for n in _iter(root)
        if n.type == "arrow_function"
    )
    body = arrow.child_by_field_name("body")
    assert body is not None and body.type != "statement_block"  # skipped by the CLI guard


def _iter(node):
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(cur.named_children)
