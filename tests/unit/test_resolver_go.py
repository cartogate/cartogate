"""Go resolver: import/selector/same-package binding + the honest unresolved ceiling."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_go import GoResolver

_GO_MOD = "module example.com/app\n\ngo 1.21\n"
_MODELS = (
    "package models\n"
    "type Base struct { Name string }\n"
    "type User struct {\n\tBase\n}\n"
    "func NewUser(name string) *User { return &User{} }\n"
    "func (u *User) Greet() string { return u.Name }\n"
)
_AUTH = (
    "package auth\n"
    'import (\n\t"fmt"\n\t"example.com/app/models"\n)\n'
    "func Authenticate(name string) bool { return validate(name) }\n"
    "func validate(name string) bool { return len(name) > 0 }\n"
    "func Make() *models.User {\n\tfmt.Println(\"x\")\n\treturn models.NewUser(\"x\")\n}\n"
    "func Use() {\n\tu := models.NewUser(\"x\")\n\tu.Greet()\n}\n"
)


def _build(tmp_path: Path) -> GoResolver:
    files = {"go.mod": _GO_MOD, "models/models.go": _MODELS, "auth/auth.go": _AUTH}
    sources: dict[str, str] = {}
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if rel.endswith(".go"):
            sources[str(p.resolve())] = text
    return GoResolver(tmp_path, sources)


def _pos(text: str, token: str, *, after: str = "") -> tuple[int, int]:
    start = text.index(after) if after else 0
    idx = text.index(token, start)
    return text.count("\n", 0, idx) + 1, idx - (text.rfind("\n", 0, idx) + 1)


def test_in_repo_import_resolves_to_package(tmp_path: Path) -> None:
    resolver = _build(tmp_path)
    auth = str((tmp_path / "auth/auth.go").resolve())
    line, col = _pos(_AUTH, "models", after="example.com/app/")
    r = resolver.resolve(auth, line, col)
    assert r is not None and r.def_type == "module" and r.full_name == "models"


def test_external_import_is_unresolved(tmp_path: Path) -> None:
    resolver = _build(tmp_path)
    auth = str((tmp_path / "auth/auth.go").resolve())
    line, col = _pos(_AUTH, "fmt", after='import')
    assert resolver.resolve(auth, line, col) is None


def test_same_package_call_resolves(tmp_path: Path) -> None:
    resolver = _build(tmp_path)
    auth = str((tmp_path / "auth/auth.go").resolve())
    line, col = _pos(_AUTH, "validate", after="return ")
    r = resolver.resolve(auth, line, col)
    assert r is not None and r.full_name == "auth.validate"


def test_cross_package_selector_call_resolves(tmp_path: Path) -> None:
    resolver = _build(tmp_path)
    auth = str((tmp_path / "auth/auth.go").resolve())
    line, col = _pos(_AUTH, "NewUser", after="return models.")
    r = resolver.resolve(auth, line, col)
    assert r is not None and r.full_name == "models.NewUser"


def test_struct_embedding_resolves(tmp_path: Path) -> None:
    resolver = _build(tmp_path)
    models = str((tmp_path / "models/models.go").resolve())
    line, col = _pos(_MODELS, "Base", after="struct {\n\t")
    r = resolver.resolve(models, line, col)
    assert r is not None and r.full_name == "models.Base"


def test_local_shadow_does_not_resolve_to_package_symbol(tmp_path: Path) -> None:
    # Soundness: a local `:=` binding that shadows a package-level func must NOT resolve to it —
    # the call goes through the local func value, not the package symbol (no wrong edge), mirroring
    # the TypeScript resolver's shadowing guard.
    src = (
        "package app\n"
        'func User() string { return "u" }\n'
        "func doSomething() string {\n"
        '\tUser := func() string { return "local" }\n'
        "\treturn User()\n"
        "}\n"
    )
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    p = tmp_path / "app/main.go"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    resolver = GoResolver(tmp_path, {str(p.resolve()): src})
    line, col = _pos(src, "User", after="return ")
    assert resolver.resolve(str(p.resolve()), line, col) is None


def test_var_and_param_shadow_are_unresolved(tmp_path: Path) -> None:
    # The guard covers more than `:=` — a `var` binding and a function parameter that shadow a
    # package-level func must also leave a call through them unresolved (sound, no wrong edge).
    var_src = (
        "package app\n"
        'func User() string { return "u" }\n'
        "func a() string {\n"
        '\tvar User = func() string { return "local" }\n'
        "\treturn User()\n"
        "}\n"
    )
    param_src = (
        "package app\n"
        'func format(s string) string { return s }\n'
        "func b(format func(string) string, s string) string {\n"
        "\treturn format(s)\n"
        "}\n"
    )
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    for fname, src, token, after in (
        ("v.go", var_src, "User", "return "),
        ("p.go", param_src, "format", "return "),
    ):
        p = tmp_path / "app" / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        resolver = GoResolver(tmp_path, {str(p.resolve()): src})
        line, col = _pos(src, token, after=after)
        assert resolver.resolve(str(p.resolve()), line, col) is None


def test_typed_receiver_method_call_resolves(tmp_path: Path) -> None:
    # F-69: `x.Method()` resolves when `x` has an explicitly DECLARED type (a param or `var x T`) —
    # sound, the type is declared (no inference).
    src = (
        "package app\n"
        "type Svc struct{}\n"
        "func (s Svc) Do() string { return \"x\" }\n"
        "func run(s Svc) string { return s.Do() }\n"
        "func run2() string { var s Svc; return s.Do() }\n"
    )
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    p = tmp_path / "app" / "svc.go"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    resolver = GoResolver(tmp_path, {str(p.resolve()): src})
    abs_p = str(p.resolve())
    r1 = resolver.resolve(abs_p, *_pos(src, "Do", after="run(s Svc) string { return s."))
    assert r1 is not None and r1.full_name == "app.Svc.Do"  # param receiver
    r2 = resolver.resolve(abs_p, *_pos(src, "Do", after="var s Svc; return s."))
    assert r2 is not None and r2.full_name == "app.Svc.Do"  # var receiver


def test_cross_package_typed_receiver_and_missing_method(tmp_path: Path) -> None:
    # A param of an IMPORTED type resolves its method; a method the type does NOT have stays
    # unresolved (sound ceiling — no wrong edge).
    models = "package models\ntype User struct{}\nfunc (u User) Greet() string { return \"h\" }\n"
    auth = (
        "package auth\n"
        'import "example.com/app/models"\n'
        "func use(u models.User) string { return u.Greet() }\n"
        "func bad(u models.User) string { return u.Missing() }\n"
    )
    sources: dict[str, str] = {}
    for rel, text in {"go.mod": _GO_MOD, "models/m.go": models, "auth/a.go": auth}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if rel.endswith(".go"):
            sources[str(p.resolve())] = text
    resolver = GoResolver(tmp_path, sources)
    a = str((tmp_path / "auth/a.go").resolve())
    r = resolver.resolve(a, *_pos(auth, "Greet", after="u."))
    assert r is not None and r.full_name == "models.User.Greet"  # imported-type receiver
    assert resolver.resolve(a, *_pos(auth, "Missing", after="u.")) is None  # not a method of User


def test_pointer_alias_and_package_var_receivers(tmp_path: Path) -> None:
    # Recall coverage: a *T pointer param, a non-default import alias, and a package-level `var`
    # all resolve their typed-receiver method (each declared, hence sound).
    models = "package models\ntype User struct{}\nfunc (u *User) Save() {}\n"
    main = (
        "package app\n"
        'import m "example.com/app/models"\n'
        "type Svc struct{}\n"
        "var g Svc\n"
        "func (s Svc) Do() {}\n"
        "func a(p *Svc) { p.Do() }\n"
        "func b(u m.User) { u.Save() }\n"
        "func c() { g.Do() }\n"
    )
    sources: dict[str, str] = {}
    for rel, text in {"go.mod": _GO_MOD, "models/m.go": models, "app/main.go": main}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if rel.endswith(".go"):
            sources[str(p.resolve())] = text
    resolver = GoResolver(tmp_path, sources)
    mp = str((tmp_path / "app/main.go").resolve())
    r_ptr = resolver.resolve(mp, *_pos(main, "Do", after="p."))
    assert r_ptr is not None and r_ptr.full_name == "app.Svc.Do"  # *Svc pointer param
    r_alias = resolver.resolve(mp, *_pos(main, "Save", after="u."))
    assert r_alias is not None and r_alias.full_name == "models.User.Save"  # aliased import type
    r_pkgvar = resolver.resolve(mp, *_pos(main, "Do", after="g."))
    assert r_pkgvar is not None and r_pkgvar.full_name == "app.Svc.Do"  # package-level var


def test_self_referential_var_resolves_to_outer_binding(tmp_path: Path) -> None:
    # Soundness: in `var s A = h(s.M())` the inner `s` is the OUTER `s B` (Go doesn't scope a
    # binding into its own initializer) — must resolve to B.M, NOT A.M. Mirrors the Rust review's
    # wrong-edge finding; the `end_point <= before` cutoff prevents the declaring var from capturing
    # a receiver inside its own initializer.
    src = (
        "package app\n"
        "type A struct{}\n"
        "func (a A) M() int { return 0 }\n"
        "type B struct{}\n"
        "func (b B) M() int { return 1 }\n"
        "func h(v int) A { return A{} }\n"
        "func g() int {\n"
        "\tvar s B\n\t_ = s\n"
        "\t{\n\t\tvar s A = h(s.M())\n\t\t_ = s\n\t}\n"
        "\treturn 0\n}\n"
    )
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    p = tmp_path / "app" / "main.go"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    resolver = GoResolver(tmp_path, {str(p.resolve()): src})
    r = resolver.resolve(str(p.resolve()), *_pos(src, "M", after="h(s."))
    assert r is not None and r.full_name == "app.B.M"  # the outer s B, not the declaring s A


def test_value_receiver_method_call_is_unresolved(tmp_path: Path) -> None:
    # u.Greet() — `u` is a local value (inferred type), not a package → sound ceiling.
    resolver = _build(tmp_path)
    auth = str((tmp_path / "auth/auth.go").resolve())
    line, col = _pos(_AUTH, "Greet", after="u.")
    assert resolver.resolve(auth, line, col) is None
