"""JavaScript signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

J = Language.JAVASCRIPT


def test_function_strips_keywords_keeps_param_names() -> None:
    assert normalize_signature("export function authenticate(name, pw) {}", J) == (
        "authenticate(name,pw)"
    )


def test_arrow_const_is_callable() -> None:
    assert normalize_signature("const add = (a, b) => a + b", J) == "add(a,b)"
    assert normalize_signature("export const App = (props) => {}", J) == "App(props)"


def test_async_and_default_params() -> None:
    assert normalize_signature("async function fetchUser(id) {}", J) == "fetchUser(id)"
    assert normalize_signature("function greet(name = 'x') {}", J) == "greet(name)"


def test_class_signature_is_name_with_bases() -> None:
    assert normalize_signature("export default class App", J) == "App"
    assert normalize_signature("class User", J) == "User"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("export  function  add( a ,  b )", J)
    b = normalize_signature("export function add(a, b)", J)
    assert a == b == "add(a,b)"
