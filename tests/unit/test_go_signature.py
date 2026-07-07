"""Go signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

G = Language.GO


def test_func_strips_keyword_keeps_param_names() -> None:
    assert normalize_signature("func Authenticate(name string, pw string)", G) == (
        "Authenticate(name,pw)"
    )


def test_grouped_params_and_variadic() -> None:
    assert normalize_signature("func add(a, b int)", G) == "add(a,b)"
    assert normalize_signature("func log(parts ...string)", G) == "log(parts)"


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("type User struct{}", G) == "User"
    assert normalize_signature("type Greeter interface{}", G) == "Greeter"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("func  add( a ,  b  int )", G)
    b = normalize_signature("func add(a, b int)", G)
    assert a == b == "add(a,b)"
