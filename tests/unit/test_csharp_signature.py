"""C# signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

C = Language.CSHARP


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("public class User", C) == "User"
    assert normalize_signature("public sealed class User", C) == "User"
    assert normalize_signature("internal struct Point", C) == "Point"
    assert normalize_signature("public interface IAuth", C) == "IAuth"


def test_method_signature_is_by_parameter_type() -> None:
    # C# overloads differ by parameter type; the walker emits ``name(Type name)`` and the canonical
    # key keeps the leading type token, so ``Add(int,int)`` / ``Add(double,double)`` stay distinct.
    assert normalize_signature("Authenticate(string name)", C) == "Authenticate(string)"
    assert normalize_signature("Add(int a, int b)", C) == "Add(int,int)"
    assert normalize_signature("Add(int a, int b)", C) != normalize_signature(
        "Add(double a, double b)", C
    )
    # The param NAME no longer affects the key.
    assert normalize_signature("M(int x)", C) == normalize_signature("M(int y)", C)


def test_generics_are_depth_protected_not_split_on_inner_comma() -> None:
    assert normalize_signature("Find(Dictionary<string,int> m, int x)", C) == (
        "Find(Dictionary<string,int>,int)"
    )


def test_no_params_keeps_empty_list() -> None:
    assert normalize_signature("Make()", C) == "Make()"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("public   bool  Authenticate( string  name )", C)
    b = normalize_signature("public bool Authenticate(string name)", C)
    assert a == b
