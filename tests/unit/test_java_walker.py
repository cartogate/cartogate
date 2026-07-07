"""Structural facts the Java walker emits (no resolution)."""

from __future__ import annotations

from cartogate.extract.ast_walker import NAME_CALL, NAME_IMPORT, NAME_INHERIT
from cartogate.extract.java_walker import JavaWalker
from cartogate.schema.enums import NodeKind, Visibility

_SOURCE = """package app.svc;

import app.models.User;

public class Service extends Base implements Greeter {
    private String secret;

    public Service(String secret) { this.secret = secret; }

    public User run(int x) { return new User("a"); }

    protected String helper() { return secret; }

    static int add(int a, int b) { return a + b; }
}

interface Greeter { String greet(); }

enum Color { RED, GREEN }
"""


def _walk():
    return JavaWalker().walk(
        _SOURCE.encode("utf-8"), module_qname="app.svc", rel_path="app/svc/Service.java",
        abs_path="/x/app/svc/Service.java",
    )


def test_extracts_types_methods_interfaces_enums() -> None:
    syms = {s.qualified_name for s in _walk().symbols}
    assert "app.svc.Service" in syms
    assert "app.svc.Service.run" in syms
    assert "app.svc.Service.helper" in syms
    assert "app.svc.Service.Service" in syms  # the constructor
    assert "app.svc.Greeter" in syms
    assert "app.svc.Color" in syms


def test_container_qnames_distinguish_top_level_from_members() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["app.svc.Service"].container_qname == "app.svc"  # top-level → module/package
    assert by_name["app.svc.Service.run"].container_qname == "app.svc.Service"  # method → class
    assert all(s.kind is NodeKind.SYMBOL for s in _walk().symbols)


def test_visibility_from_access_modifiers() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["app.svc.Service.run"].visibility is Visibility.PUBLIC
    assert by_name["app.svc.Service.helper"].visibility is Visibility.EXPORTED  # protected
    assert by_name["app.svc.Service.add"].visibility is Visibility.INTERNAL  # package-private


def test_type_signature_is_the_bare_name() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    # A type's gate signature is its name (re-declaring `class Service` is a duplicate).
    assert by_name["app.svc.Service"].signature == "Service"
    # A method keeps its raw parameter text for later normalization.
    assert by_name["app.svc.Service.run"].signature == "run(int x)"


def test_emits_name_occurrences_for_resolution() -> None:
    relations = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_IMPORT, "User") in relations  # import app.models.User
    assert (NAME_INHERIT, "Base") in relations  # extends Base
    assert (NAME_INHERIT, "Greeter") in relations  # implements Greeter
    assert (NAME_CALL, "User") in relations  # new User(...)
