"""Unit tests for the C# structural walker (symbols, qnames, visibility, name occurrences)."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
)
from cartogate.extract.csharp_walker import CSharpWalker
from cartogate.schema.enums import Visibility

SOURCE = b"""using System;
using App.Models;

namespace App.Services
{
    public class AuthService : BaseService, IAuth
    {
        private readonly User _user;

        public AuthService(User u) { _user = u; }

        public bool Authenticate(string name) { return Validate(name) && _user.IsActive(); }

        private bool Validate(string n) { return true; }

        public static User Make() { return new User("a"); }
    }

    internal interface IAuth { bool Authenticate(string name); }
}
"""


def _walk():
    return CSharpWalker().walk(
        SOURCE, module_qname="Services", rel_path="Services.cs", abs_path="/x/Services.cs"
    )


def test_symbols_and_qnames() -> None:
    facts = _walk()
    by_qname = {s.qualified_name: s for s in facts.symbols}
    # A type in a namespace is still a file-module-based qname (the namespace is transparent here).
    assert "Services.AuthService" in by_qname
    assert "Services.AuthService.Authenticate" in by_qname
    assert "Services.IAuth" in by_qname
    # The enclosing type is the method's container; the module is the type's container.
    assert by_qname["Services.AuthService"].container_qname == "Services"
    assert by_qname["Services.AuthService.Authenticate"].container_qname == "Services.AuthService"


def test_visibility_from_access_modifiers() -> None:
    by_qname = {s.qualified_name: s for s in _walk().symbols}
    assert by_qname["Services.AuthService"].visibility is Visibility.PUBLIC
    assert by_qname["Services.AuthService.Validate"].visibility is Visibility.INTERNAL  # private
    assert by_qname["Services.IAuth"].visibility is Visibility.EXPORTED  # internal


def test_method_signatures_carry_params() -> None:
    by_qname = {s.qualified_name: s for s in _walk().symbols}
    assert by_qname["Services.AuthService.Authenticate"].signature == "Authenticate(string name)"
    assert by_qname["Services.AuthService.Make"].signature == "Make()"


def test_top_level_only_for_types() -> None:
    facts = _walk()
    top = {s.qualified_name for s in facts.symbols if s.container_qname == "Services"}
    assert "Services.AuthService" in top and "Services.IAuth" in top
    assert "Services.AuthService.Authenticate" not in top  # a method is never top-level


def test_name_occurrences() -> None:
    facts = _walk()
    rels = {(n.relation, n.text) for n in facts.names}
    assert (NAME_INHERIT, "BaseService") in rels
    assert (NAME_INHERIT, "IAuth") in rels
    assert (NAME_IMPORT, "System") in rels  # external
    assert (NAME_CALL, "Validate") in rels  # same-class call
    assert (NAME_CALL, "IsActive") in rels  # receiver method call
    assert (NAME_CALL, "User") in rels  # `new User(...)` -> the constructor's type
    assert (NAME_REFERENCE, "User") in rels  # `User _user` field / param / return type


def test_using_import_carries_full_namespace() -> None:
    facts = _walk()
    models = next(n for n in facts.names if n.relation == NAME_IMPORT and n.text == "Models")
    assert models.module == "App.Models"  # the full using target, for external naming
