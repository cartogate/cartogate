"""Kotlin signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

K = Language.KOTLIN


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("class User", K) == "User"
    assert normalize_signature("open class User", K) == "User"
    assert normalize_signature("interface Logger", K) == "Logger"
    assert normalize_signature("User", K) == "User"  # the walker emits the bare type name


def test_function_signature_is_name_first() -> None:
    # Kotlin params are ``name: Type`` (name-first, like TS), so the key is the parameter name.
    assert normalize_signature("makeUser(name: String)", K) == "makeUser(name)"
    assert normalize_signature("f(a: Int, b: Int)", K) == "f(a,b)"
    assert normalize_signature("isActive()", K) == "isActive()"


def test_modifiers_stripped_from_snippet_path() -> None:
    assert normalize_signature("fun makeUser(name: String)", K) == "makeUser(name)"
    assert normalize_signature("private suspend fun run()", K) == "run()"


def test_whitespace_insensitive_and_deterministic() -> None:
    assert normalize_signature("f( a: Int , b: Int )", K) == normalize_signature(
        "f(a: Int,b: Int)", K
    )
