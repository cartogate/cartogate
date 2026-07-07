"""Rust resolver: use/path/associated-call binding + the honest unresolved ceiling."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_rust import RustResolver

_MODELS = (
    "pub mod sub;\n\n"
    "pub trait Greeter {\n    fn greet(&self) -> String;\n}\n\n"
    "pub struct User {\n    pub name: String,\n}\n\n"
    "impl User {\n    pub fn new(name: String) -> User { User { name } }\n}\n\n"
    "impl Greeter for User {\n    fn greet(&self) -> String { self.name.clone() }\n}\n"
)
_SUB = (
    "pub fn build(name: String) -> super::User {\n"
    "    super::User::new(name)\n"
    "}\n"
)
_AUTH = (
    "use crate::models::User;\n"
    "use std::fmt;\n\n"
    "pub fn authenticate(name: String) -> User {\n"
    "    let ok = validate(&name);\n"
    "    make_user(name)\n"
    "}\n\n"
    "fn validate(name: &str) -> bool { !name.is_empty() }\n\n"
    "fn make_user(name: String) -> User { User::new(name) }\n\n"
    "fn via_self(name: String) -> bool { self::validate(&name) }\n\n"
    "fn via_crate(name: String) -> User { crate::models::User::new(name) }\n\n"
    "fn use_method(u: User) -> String { u.greet() }\n\n"
    "fn shadowed() -> bool {\n"
    "    let validate = |x: &str| x.is_empty();\n"
    "    validate(\"x\")\n"
    "}\n"
)


def _build(tmp_path: Path) -> RustResolver:
    files = {"models/mod.rs": _MODELS, "models/sub.rs": _SUB, "auth.rs": _AUTH}
    sources: dict[str, str] = {}
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        sources[str(p.resolve())] = text
    return RustResolver(tmp_path, sources)


def _pos(text: str, token: str, *, after: str = "") -> tuple[int, int]:
    start = text.index(after) + len(after) if after else 0
    idx = text.index(token, start)
    return text.count("\n", 0, idx) + 1, idx - (text.rfind("\n", 0, idx) + 1)


def _auth(tmp_path: Path) -> str:
    return str((tmp_path / "auth.rs").resolve())


def test_in_repo_use_resolves_to_symbol(tmp_path: Path) -> None:
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "User", after="use crate::models::"))
    assert r is not None and r.full_name == "crate.models.User"


def test_external_use_is_unresolved(tmp_path: Path) -> None:
    assert _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "fmt", after="use std::")) is None


def test_same_module_call_resolves(tmp_path: Path) -> None:
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "validate", after="let ok = "))
    assert r is not None and r.full_name == "crate.auth.validate"


def test_associated_call_via_imported_type_resolves(tmp_path: Path) -> None:
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "new", after="fn make_user"))
    assert r is not None and r.full_name == "crate.models.User.new"


def test_self_path_call_resolves(tmp_path: Path) -> None:
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "validate", after="self::"))
    assert r is not None and r.full_name == "crate.auth.validate"


def test_crate_path_call_resolves(tmp_path: Path) -> None:
    pos = _pos(_AUTH, "new", after="crate::models::User::")
    r = _build(tmp_path).resolve(_auth(tmp_path), *pos)
    assert r is not None and r.full_name == "crate.models.User.new"


def test_super_path_resolves(tmp_path: Path) -> None:
    sub = str((tmp_path / "models/sub.rs").resolve())
    r = _build(tmp_path).resolve(sub, *_pos(_SUB, "new", after="super::User::"))
    assert r is not None and r.full_name == "crate.models.User.new"


def test_type_reference_resolves(tmp_path: Path) -> None:
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "User", after="-> "))
    assert r is not None and r.full_name == "crate.models.User"


def test_impl_trait_for_type_resolves_trait(tmp_path: Path) -> None:
    models = str((tmp_path / "models/mod.rs").resolve())
    r = _build(tmp_path).resolve(models, *_pos(_MODELS, "Greeter", after="impl "))
    assert r is not None and r.full_name == "crate.models.Greeter"


def test_param_typed_receiver_method_call_resolves(tmp_path: Path) -> None:
    # F-69: u.greet() resolves — `u: User` is a param with a DECLARED type (sound, no inference).
    # (greet comes from `impl Greeter for User`, indexed under User.)
    r = _build(tmp_path).resolve(_auth(tmp_path), *_pos(_AUTH, "greet", after="u."))
    assert r is not None and r.full_name == "crate.models.User.greet"


def test_let_typed_receivers_resolve_and_inference_stays_unresolved(tmp_path: Path) -> None:
    # F-69 soundness: a `let x: T` annotation and a `let x = T {..}` struct literal give an explicit
    # type (resolve); a `let x = make()` (a function's return value) is INFERENCE — left unresolved.
    src = (
        "pub struct Svc { pub n: i32 }\n"
        "impl Svc { pub fn run(&self) -> i32 { self.n } }\n"
        "fn make() -> Svc { Svc { n: 0 } }\n"
        "fn a() -> i32 { let s: Svc = make(); s.run() }\n"
        "fn b() -> i32 { let s = Svc { n: 1 }; s.run() }\n"
        "fn c() -> i32 { let s = make(); s.run() }\n"
        "fn d(s: Svc) -> i32 { s.missing() }\n"
    )
    p = tmp_path / "lib.rs"
    p.write_text(src, encoding="utf-8")
    resolver = RustResolver(tmp_path, {str(p.resolve()): src})
    lp = str(p.resolve())
    r_ann = resolver.resolve(lp, *_pos(src, "run", after="let s: Svc = make(); s."))
    assert r_ann is not None and r_ann.full_name == "crate.Svc.run"  # annotation
    r_lit = resolver.resolve(lp, *_pos(src, "run", after="Svc { n: 1 }; s."))
    assert r_lit is not None and r_lit.full_name == "crate.Svc.run"  # struct literal
    assert resolver.resolve(lp, *_pos(src, "run", after="let s = make(); s.")) is None  # inferred
    assert resolver.resolve(lp, *_pos(src, "missing", after="s.")) is None  # not a method


def test_self_referential_let_resolves_to_outer_binding(tmp_path: Path) -> None:
    # Soundness: in `let x: A = h(x.m())` the inner `x` is the OUTER `x: B` (Rust doesn't scope a
    # binding into its own initializer) — must resolve to B.m, NOT A.m (the wrong-edge the review
    # caught: a `start_point` cutoff would let the declaring `let` capture a receiver in its value).
    src = (
        "pub struct A {}\nimpl A { pub fn m(&self) -> i32 { 0 } }\n"
        "pub struct B {}\nimpl B { pub fn m(&self) -> i32 { 1 } }\n"
        "fn h(v: i32) -> A { A {} }\n"
        "fn g() -> i32 { let x: B = B {}; let x: A = h(x.m()); 0 }\n"
    )
    p = tmp_path / "lib.rs"
    p.write_text(src, encoding="utf-8")
    resolver = RustResolver(tmp_path, {str(p.resolve()): src})
    r = resolver.resolve(str(p.resolve()), *_pos(src, "m", after="h(x."))
    assert r is not None and r.full_name == "crate.B.m"  # the outer x: B, not the declaring x: A


def test_local_shadow_is_unresolved(tmp_path: Path) -> None:
    # A `let` binding shadows the module-level `validate`; the call goes through the closure,
    # so it must NOT resolve to the function symbol (sound, no wrong edge).
    pos = _pos(_AUTH, "validate", after="|x: &str| x.is_empty();\n    ")
    assert _build(tmp_path).resolve(_auth(tmp_path), *pos) is None
