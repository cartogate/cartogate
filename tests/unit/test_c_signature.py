"""C signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

C = Language.C


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("struct User", C) == "User"
    assert normalize_signature("Point", C) == "Point"  # a typedef name


def test_function_signature_keeps_params() -> None:
    # C has no overloading; the gate keys a function on its name + (normalized) parameter list.
    assert normalize_signature("authenticate(struct User *u, const char *name)", C) == (
        "authenticate(struct,const)"
    )
    assert normalize_signature("run()", C) == "run()"


def test_type_decl_keywords_are_stripped() -> None:
    # The walker emits ``name(params)`` (no leading keywords); a raw type signature on the snippet
    # path still strips the ``struct``/``union``/``enum`` lead to the bare type name.
    assert normalize_signature("struct User", C) == "User"
    assert normalize_signature("union Variant", C) == "Variant"
    assert normalize_signature("enum Color", C) == "Color"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("authenticate( struct User *u )", C)
    b = normalize_signature("authenticate(struct User *u)", C)
    assert a == b
