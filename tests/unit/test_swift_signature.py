"""Swift signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

S = Language.SWIFT


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("class User", S) == "User"
    assert normalize_signature("public class User", S) == "User"
    assert normalize_signature("struct Point", S) == "Point"
    assert normalize_signature("protocol Logger", S) == "Logger"
    assert normalize_signature("User", S) == "User"  # the walker emits the bare type name


def test_function_signature_is_name_first() -> None:
    # Swift params are ``label name: Type`` (name-first), so the key is the parameter label.
    assert normalize_signature("makeUser(name: String)", S) == "makeUser(name)"
    assert normalize_signature("m(u: User, n: Int)", S) == "m(u,n)"
    assert normalize_signature("isActive()", S) == "isActive()"


def test_modifiers_stripped_from_snippet_path() -> None:
    assert normalize_signature("func makeUser(name: String)", S) == "makeUser(name)"
    assert normalize_signature("private final func run()", S) == "run()"


def test_whitespace_insensitive_and_deterministic() -> None:
    assert normalize_signature("m( u: User , n: Int )", S) == normalize_signature(
        "m(u: User,n: Int)", S
    )
