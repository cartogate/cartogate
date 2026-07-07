"""Java signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

J = Language.JAVA


def test_method_signature_is_by_parameter_type_not_name() -> None:
    # Java overloads are distinguished by parameter TYPE, not name — so the canonical key keeps the
    # types (modifiers/return type dropped), letting `add(int)` and `add(String)` stay distinct.
    assert normalize_signature("public static boolean authenticate(String name)", J) == (
        "authenticate(String)"
    )
    assert normalize_signature("int add(int a, int b)", J) == "add(int,int)"
    # The param NAME no longer affects the key — both name-variants collapse identically...
    assert normalize_signature("void f(int x)", J) == normalize_signature("void f(int y)", J)
    # ...but a different TYPE makes a distinct key (the whole point).
    assert normalize_signature("void f(int x)", J) != normalize_signature("void f(long x)", J)


def test_generics_erased_qualifiers_stripped_arrays_and_varargs_kept() -> None:
    # Generics are erased (you can't overload on them), package qualifiers reduce to the simple
    # name, arrays keep `[]`, and varargs normalize to the array form (`T...` == `T[]`).
    assert normalize_signature("List<String> find(Map<String,Integer> m, int[] xs)", J) == (
        "find(Map,int[])"
    )
    assert normalize_signature("void log(String... parts)", J) == "log(String[])"
    assert normalize_signature("void use(java.util.List items)", J) == "use(List)"


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("public class User", J) == "User"
    assert normalize_signature("public class User {}", J) == "User"
    assert normalize_signature("interface Greeter", J) == "Greeter"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("public   int  add( int a ,  int b )", J)
    b = normalize_signature("public int add(int a, int b)", J)
    assert a == b == "add(int,int)"


def test_no_params_keeps_empty_list() -> None:
    assert normalize_signature("void run()", J) == "run()"
    assert normalize_signature("String toString()", J) == "toString()"


def test_modifiers_annotations_and_c_style_array() -> None:
    assert normalize_signature("void m(final int x)", J) == "m(int)"  # `final` dropped
    assert normalize_signature("void m(@NonNull String s)", J) == "m(String)"  # annotation dropped
    assert normalize_signature("void m(final @Valid Foo f)", J) == "m(Foo)"  # both dropped
    assert normalize_signature("void m(int xs[])", J) == "m(int[])"  # C-style array binds to type
