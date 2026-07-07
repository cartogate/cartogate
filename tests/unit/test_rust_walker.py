"""Structural facts the Rust walker emits (no resolution)."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
)
from cartogate.extract.rust_walker import RustWalker
from cartogate.schema.enums import Visibility

_SOURCE = """use crate::models::User;
use std::fmt;

pub trait Greeter {
    fn greet(&self) -> String;
}

pub struct Account {
    pub name: String,
}

impl Account {
    pub fn new(name: String) -> Account {
        User::from(name)
    }

    fn secret(&self) -> i32 {
        0
    }
}

impl Greeter for Account {
    fn greet(&self) -> String {
        helper()
    }
}

fn helper() -> i32 {
    0
}
"""


def _walk():
    return RustWalker().walk(
        _SOURCE.encode("utf-8"), module_qname="crate.auth", rel_path="auth.rs",
        abs_path="/x/auth.rs",
    )


def test_extracts_fns_types_and_methods() -> None:
    syms = {s.qualified_name for s in _walk().symbols}
    assert "crate.auth.helper" in syms  # free function → top-level
    assert {"crate.auth.Account", "crate.auth.Greeter"} <= syms  # struct + trait types
    assert "crate.auth.Account.new" in syms  # impl method → container is the type
    assert "crate.auth.Account.secret" in syms
    assert "crate.auth.Greeter.greet" in syms  # trait method signature nests under the trait


def test_container_qnames() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["crate.auth.helper"].container_qname == "crate.auth"  # fn → module (top-level)
    assert by_name["crate.auth.Account"].container_qname == "crate.auth"  # type → module
    assert by_name["crate.auth.Account.new"].container_qname == "crate.auth.Account"  # → the type


def test_visibility_by_pub() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["crate.auth.Account"].visibility is Visibility.PUBLIC  # `pub`
    assert by_name["crate.auth.Account.new"].visibility is Visibility.PUBLIC
    assert by_name["crate.auth.Account.secret"].visibility is Visibility.INTERNAL  # no modifier
    assert by_name["crate.auth.helper"].visibility is Visibility.INTERNAL


def test_pub_crate_is_exported() -> None:
    src = "pub(crate) fn shared() {}\npub(super) struct Inner {}\n"
    by_name = {s.qualified_name: s for s in RustWalker().walk(
        src.encode("utf-8"), module_qname="crate.m", rel_path="m.rs", abs_path="/x/m.rs",
    ).symbols}
    assert by_name["crate.m.shared"].visibility is Visibility.EXPORTED
    assert by_name["crate.m.Inner"].visibility is Visibility.EXPORTED


def test_signatures() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["crate.auth.Account"].signature == "Account"  # type → bare name
    assert by_name["crate.auth.Account.new"].signature == "new(name: String)"  # keeps param text
    assert by_name["crate.auth.Account.secret"].signature == "secret(&self)"  # raw self kept here


def test_name_occurrences() -> None:
    relations = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_IMPORT, "User") in relations  # use crate::models::User (local name)
    assert (NAME_IMPORT, "fmt") in relations  # use std::fmt (external)
    assert (NAME_INHERIT, "Greeter") in relations  # impl Greeter for Account
    assert (NAME_CALL, "helper") in relations  # same-module call
    assert (NAME_CALL, "from") in relations  # User::from associated call → rightmost segment
    # The use target `User` is recorded as an import occurrence (the type usage is excluded there).
    assert (NAME_REFERENCE, "String") in relations  # a type position
