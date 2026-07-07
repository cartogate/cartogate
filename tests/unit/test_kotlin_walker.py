"""Unit tests for the Kotlin structural walker (types, functions, inherits, calls, imports)."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
)
from cartogate.extract.kotlin_walker import KotlinWalker
from cartogate.schema.enums import Visibility

SOURCE = b"""package app.models

import app.util.Logger

open class Base {
    open fun init() {}
}

class User(val name: String) : Base(), Logger {
    fun isActive(): Boolean {
        return validate(name)
    }
    private fun secret() {}
}

object Registry {
    fun register() {}
}

fun makeUser(name: String): User {
    return User(name)
}
"""


def _walk():
    return KotlinWalker().walk(
        SOURCE, module_qname="models", rel_path="models.kt", abs_path="/x/models.kt"
    )


def test_types_methods_and_top_level_function() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert {"models.Base", "models.User", "models.Registry"} <= set(by)  # class + object
    assert by["models.User.isActive"].container_qname == "models.User"  # a method
    assert by["models.makeUser"].container_qname == "models"  # a top-level function (module-level)


def test_visibility() -> None:
    by = {s.qualified_name: s for s in _walk().symbols}
    assert by["models.User"].visibility is Visibility.PUBLIC  # Kotlin default
    assert by["models.User.secret"].visibility is Visibility.INTERNAL  # private


def test_inherit_import_call_and_reference_occurrences() -> None:
    rels = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_INHERIT, "Base") in rels  # base class
    assert (NAME_INHERIT, "Logger") in rels  # interface
    assert (NAME_IMPORT, "Logger") in rels
    assert (NAME_CALL, "validate") in rels  # unqualified call
    assert (NAME_CALL, "User") in rels  # `User(name)` constructor call
    assert (NAME_REFERENCE, "String") in rels  # the `val name: String` class-parameter type


def test_import_carries_full_package() -> None:
    imp = next(n for n in _walk().names if n.relation == NAME_IMPORT and n.text == "Logger")
    assert imp.module == "app.util.Logger"
