"""C++ signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

C = Language.CPP


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("class User", C) == "User"
    assert normalize_signature("struct Point", C) == "Point"
    assert normalize_signature("User", C) == "User"  # the walker emits the bare class name


def test_method_signature_keeps_params() -> None:
    assert normalize_signature("isActive()", C) == "isActive()"
    assert normalize_signature("add(int a, int b)", C) == "add(int,int)"


def test_decl_keywords_stripped_from_snippet_path() -> None:
    # The walker emits ``name(params)``; a raw snippet with leading keywords reduces to the name.
    assert normalize_signature("class User", C) == "User"
    assert normalize_signature("namespace app", C) == "app"


def test_whitespace_insensitive_and_deterministic() -> None:
    assert normalize_signature("add( int a , int b )", C) == normalize_signature(
        "add(int a,int b)", C
    )
