"""Rust signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

R = Language.RUST


def test_fn_strips_keywords_keeps_param_names() -> None:
    assert normalize_signature("pub fn authenticate(name: String, pw: String)", R) == (
        "authenticate(name,pw)"
    )


def test_visibility_and_qualifiers_stripped() -> None:
    assert normalize_signature("pub(crate) async unsafe fn run(a: i32)", R) == "run(a)"
    assert normalize_signature("const fn add(a: i32, b: i32)", R) == "add(a,b)"


def test_self_receiver_dropped() -> None:
    assert normalize_signature("fn greet(&self) -> String", R) == "greet()"
    assert normalize_signature("fn set(&mut self, v: i32)", R) == "set(v)"
    assert normalize_signature("fn take(self, v: i32)", R) == "take(v)"


def test_ref_and_mut_params_keep_name() -> None:
    assert normalize_signature("fn validate(name: &str)", R) == "validate(name)"
    assert normalize_signature("fn bump(mut count: i32)", R) == "bump(count)"


def test_type_signature_is_bare_name() -> None:
    assert normalize_signature("pub struct User", R) == "User"
    assert normalize_signature("pub trait Greeter", R) == "Greeter"
    assert normalize_signature("enum State", R) == "State"


def test_generic_params_not_split_on_inner_comma() -> None:
    assert normalize_signature("fn merge(m: Map<String, i32>, n: i32)", R) == "merge(m,n)"


def test_whitespace_insensitive_and_deterministic() -> None:
    a = normalize_signature("pub  fn  add( a : i32 ,  b : i32 )", R)
    b = normalize_signature("pub fn add(a: i32, b: i32)", R)
    assert a == b == "add(a,b)"
