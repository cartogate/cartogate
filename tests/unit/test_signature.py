"""Gate tests for signature normalization (Section 1).

``normalize_signature`` maps both stored symbol signatures and incoming
``check_duplicate`` queries to one canonical key, so the duplicate gate compares
like with like. It must be deterministic, whitespace-insensitive, and robust to
annotations/defaults that contain commas (depth-aware parameter splitting).
"""

from __future__ import annotations

import pytest

from cartogate.schema.signature import normalize_signature


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("def authenticate(self, user, password) -> bool:", "authenticate(user,password)"),
        ("async def fetch(url: str, *, timeout: float = 5.0)", "fetch(url,timeout)"),
        ("  authenticate( user , password )  ", "authenticate(user,password)"),
        ("foo()", "foo()"),
        ("def handler(*args, **kwargs)", "handler(args,kwargs)"),
        # Commas inside annotations/defaults must not be treated as param separators.
        ("build(x: Dict[str, int], y=[1, 2, 3])", "build(x,y)"),
        ("nested(a: Callable[[int, str], bool], b)", "nested(a,b)"),
        # Positional-only (/) and keyword-only (*) markers are dropped.
        ("g(a, b, /, c, *, d)", "g(a,b,c,d)"),
        # Class definition: name + bases, same canonical shape.
        ("class UserService(BaseService):", "UserService(BaseService)"),
    ],
)
def test_normalize_signature_cases(raw: str, expected: str) -> None:
    assert normalize_signature(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize_signature("def authenticate(self, user, password) -> bool:")
    assert normalize_signature(once) == once


def test_name_only_returns_bare_name() -> None:
    assert normalize_signature("  authenticate ") == "authenticate"


def test_two_spellings_of_same_function_collide() -> None:
    a = normalize_signature("def authenticate(self, user , password)->bool")
    b = normalize_signature("authenticate(user, password)")
    assert a == b


def test_different_arity_does_not_collide() -> None:
    assert normalize_signature("foo(a)") != normalize_signature("foo(a, b)")
