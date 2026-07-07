"""Unit tests for the tree-sitter walker — scope-derived qualified names + relations.

Isolated from name resolution: these assert the structural facts (symbols, qnames, and
the *positions/relations* to be resolved) the walker produces from source alone.
"""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
    TreeSitterWalker,
)

SOURCE = b'''
import os

from .models import User


class Base:
    def greet(self):
        return "hi"


class Service(Base):
    def run(self, x):
        return helper(x)


def helper(x):
    DEFAULT = User
    return x
'''


def _walk():
    return TreeSitterWalker().walk(
        SOURCE, module_qname="pkg.svc", rel_path="pkg/svc.py", abs_path="/abs/pkg/svc.py"
    )


def test_symbols_have_scoped_qualified_names() -> None:
    facts = _walk()
    qnames = {s.qualified_name for s in facts.symbols}
    assert qnames == {
        "pkg.svc.Base",
        "pkg.svc.Base.greet",
        "pkg.svc.Service",
        "pkg.svc.Service.run",
        "pkg.svc.helper",
    }


def test_method_container_is_the_class() -> None:
    facts = _walk()
    run = next(s for s in facts.symbols if s.name == "run")
    assert run.container_qname == "pkg.svc.Service"
    greet = next(s for s in facts.symbols if s.name == "greet")
    assert greet.container_qname == "pkg.svc.Base"


def test_relations_captured_by_kind() -> None:
    facts = _walk()
    relations = {(n.relation, n.text) for n in facts.names}
    assert (NAME_INHERIT, "Base") in relations  # class Service(Base)
    assert (NAME_CALL, "helper") in relations  # helper(x) inside run
    assert (NAME_IMPORT, "os") in relations  # import os
    assert (NAME_IMPORT, "User") in relations  # from .models import User
    # DEFAULT = User is a name load resolving later to a reference.
    assert (NAME_REFERENCE, "User") in relations


def test_enclosing_symbol_for_call_is_the_method() -> None:
    facts = _walk()
    call = next(n for n in facts.names if n.relation == NAME_CALL and n.text == "helper")
    assert call.enclosing_qname == "pkg.svc.Service.run"
