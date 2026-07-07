"""Unit tests for the C structural walker (functions, types, includes, calls, references)."""

from __future__ import annotations

from cartogate.extract.ast_walker import NAME_CALL, NAME_IMPORT, NAME_REFERENCE
from cartogate.extract.c_walker import CWalker
from cartogate.schema.enums import Visibility

SOURCE = b"""#include <stdio.h>
#include "user.h"

typedef struct Point { int x; int y; } Point;

struct User { char *name; int age; };

int authenticate(struct User *u, const char *name) {
    int ok = validate(name);
    Point p = make_point(1, 2);
    return create_user(u);
}

static int validate(const char *name) { return name != 0; }
"""


def _walk():
    return CWalker().walk(SOURCE, module_qname="auth", rel_path="auth.c", abs_path="/x/auth.c")


def test_function_and_type_symbols() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert {"auth.Point", "auth.User", "auth.authenticate", "auth.validate"} <= set(by)
    # Functions and types are all module-level (top-level) in C.
    assert all(by[q].container_qname == "auth" for q in by)


def test_function_signature_and_static_visibility() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert by["auth.authenticate"].signature == "authenticate(struct User *u, const char *name)"
    assert by["auth.authenticate"].visibility is Visibility.EXPORTED  # external linkage
    assert by["auth.validate"].visibility is Visibility.INTERNAL  # static -> file-local


def test_includes_emit_imports() -> None:
    names = {(n.relation, n.text, n.module) for n in _walk().names}
    assert (NAME_IMPORT, "stdio.h", "stdio.h") in names  # system header (external)
    assert (NAME_IMPORT, "user.h", "user.h") in names  # repo-relative header


def test_calls_and_references() -> None:
    rels = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_CALL, "validate") in rels  # same-TU call
    assert (NAME_CALL, "make_point") in rels
    assert (NAME_CALL, "create_user") in rels
    assert (NAME_REFERENCE, "User") in rels  # struct type used in a parameter
    assert (NAME_REFERENCE, "Point") in rels  # typedef type used in a local


def test_prototype_is_not_a_symbol() -> None:
    # A bare prototype (declaration, no body) is not a function symbol — only definitions are.
    facts = CWalker().walk(
        b"int only_declared(int a);\n", module_qname="h", rel_path="h.h", abs_path="/x/h.h"
    )
    assert not facts.symbols
