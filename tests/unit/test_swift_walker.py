"""Unit tests for the Swift structural walker (types, methods, extensions, calls, imports)."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
)
from cartogate.extract.swift_walker import SwiftWalker
from cartogate.schema.enums import Visibility

SOURCE = b"""import Foundation

protocol Logger { func log() }

class Base {
    func initialize() {}
}

class User: Base, Logger {
    let name: String
    init(name: String) { self.name = name }
    func isActive() -> Bool { return validate(name: name) }
    private func secret() {}
    func log() {}
}

func makeUser(name: String) -> User {
    return User(name: name)
}

extension User {
    func greet() -> String { return name }
}
"""


def _walk():
    return SwiftWalker().walk(
        SOURCE, module_qname="models", rel_path="Models.swift", abs_path="/x/Models.swift"
    )


def test_types_methods_and_top_level_function() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert {"models.Logger", "models.Base", "models.User"} <= set(by)  # protocol + classes
    assert by["models.User.isActive"].container_qname == "models.User"  # a method
    assert by["models.makeUser"].container_qname == "models"  # a top-level function


def test_extension_method_attaches_to_the_type() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    # `extension User { func greet() }` -> a method under User (no new type symbol for it).
    assert "models.User.greet" in by and by["models.User.greet"].container_qname == "models.User"


def test_init_is_a_method_symbol() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert "models.User.init" in by


def test_visibility() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert by["models.User"].visibility is Visibility.EXPORTED  # Swift default (internal)
    assert by["models.User.secret"].visibility is Visibility.INTERNAL  # private


def test_inherit_import_call_and_reference_occurrences() -> None:
    rels = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_INHERIT, "Base") in rels  # base class
    assert (NAME_INHERIT, "Logger") in rels  # protocol
    assert (NAME_IMPORT, "Foundation") in rels  # external module
    assert (NAME_CALL, "validate") in rels  # unqualified call
    assert (NAME_CALL, "User") in rels  # `User(name:)` initializer call
    assert (NAME_REFERENCE, "String") in rels  # the `let name: String` property type
