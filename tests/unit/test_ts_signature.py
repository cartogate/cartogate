"""TypeScript signature normalization for the duplicate gate."""

from __future__ import annotations

from cartogate.schema.enums import Language
from cartogate.schema.signature import normalize_signature

TS = Language.TYPESCRIPT


def test_function_strips_types_and_keywords() -> None:
    sig = "export function authenticate(name: string, pwd: string): boolean"
    assert normalize_signature(sig, TS) == "authenticate(name,pwd)"
    assert normalize_signature("async function load(url: string): Promise<Data>", TS) == "load(url)"


def test_arrow_const_and_methods() -> None:
    assert normalize_signature("const makeUser = (name: string): User =>", TS) == "makeUser(name)"
    assert normalize_signature("greet(): string", TS) == "greet()"
    assert normalize_signature("private promote(by: number = 1): void", TS) == "promote(by)"


def test_generics_optionals_and_rest_params() -> None:
    generic = "function find<T>(id: string, opts?: Opts): T"
    assert normalize_signature(generic, TS) == "find(id,opts)"
    assert normalize_signature("function merge(...parts: string[]): string", TS) == "merge(parts)"


def test_class_and_interface() -> None:
    assert normalize_signature("export class Admin", TS) == "Admin"
    assert normalize_signature("interface Repo<T>", TS) == "Repo"


def test_is_deterministic_and_whitespace_insensitive() -> None:
    a = normalize_signature("function  foo( a : number ,  b : string ) : void", TS)
    b = normalize_signature("function foo(a: number, b: string): void", TS)
    assert a == b == "foo(a,b)"


def test_typescript_does_not_strip_self_param() -> None:
    # `self`/`cls` are Python receivers; in TS they are ordinary params and must be kept.
    assert normalize_signature("function f(self: T): void", TS) == "f(self)"
