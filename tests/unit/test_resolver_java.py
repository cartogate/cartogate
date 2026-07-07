"""Java resolver: import/type/call binding + the honest unresolved ceiling."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_java import JavaResolver

_FILES = {
    "app/models/Base.java": "package app.models;\npublic class Base { public String greet(){return \"h\";} }\n",  # noqa: E501
    "app/models/User.java": (
        "package app.models;\n"
        "public class User extends Base {\n"
        "  public User(String n) {}\n"
        "  public String who() { return helper(); }\n"
        "  private String helper() { return \"x\"; }\n"
        "}\n"
    ),
    "app/auth/Auth.java": (
        "package app.auth;\n"
        "import app.models.User;\n"
        "import java.util.List;\n"
        "public class Auth {\n"
        "  public static User make(String n) { return new User(n); }\n"
        "  public void use(List items) { items.size(); }\n"
        "}\n"
    ),
}


def _build(tmp_path: Path) -> tuple[JavaResolver, dict[str, str]]:
    sources: dict[str, str] = {}
    for rel, text in _FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        sources[str(p.resolve())] = text
    return JavaResolver(tmp_path, sources), {str((tmp_path / r).resolve()): r for r in _FILES}


def _pos(text: str, token: str, *, after: str = "") -> tuple[int, int]:
    """1-based line, 0-based column of ``token`` (optionally after a marker substring)."""
    start = text.index(after) if after else 0
    idx = text.index(token, start)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    return line, col


def test_import_resolves_in_repo_type(tmp_path: Path) -> None:
    resolver, _ = _build(tmp_path)
    auth = str((tmp_path / "app/auth/Auth.java").resolve())
    line, col = _pos(_FILES["app/auth/Auth.java"], "User", after="import app.models.")
    r = resolver.resolve(auth, line, col)
    assert r is not None and r.def_type == "class"
    assert r.def_path is not None and r.def_path.name == "User.java"


def test_external_import_is_unresolved(tmp_path: Path) -> None:
    resolver, _ = _build(tmp_path)
    auth = str((tmp_path / "app/auth/Auth.java").resolve())
    line, col = _pos(_FILES["app/auth/Auth.java"], "List", after="import java.util.")
    assert resolver.resolve(auth, line, col) is None  # external → external node upstream


def test_extends_resolves_base(tmp_path: Path) -> None:
    resolver, _ = _build(tmp_path)
    user = str((tmp_path / "app/models/User.java").resolve())
    line, col = _pos(_FILES["app/models/User.java"], "Base", after="extends ")
    r = resolver.resolve(user, line, col)
    assert r is not None and r.def_path is not None and r.def_path.name == "Base.java"


def test_new_resolves_constructed_type(tmp_path: Path) -> None:
    resolver, _ = _build(tmp_path)
    auth = str((tmp_path / "app/auth/Auth.java").resolve())
    line, col = _pos(_FILES["app/auth/Auth.java"], "User", after="new ")
    r = resolver.resolve(auth, line, col)
    assert r is not None and r.def_type == "class" and r.def_path.name == "User.java"


def _build_one(tmp_path: Path, rel: str, src: str, *extra: tuple[str, str]) -> JavaResolver:
    sources: dict[str, str] = {}
    for r, text in ((rel, src), *extra):
        p = tmp_path / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        sources[str(p.resolve())] = text
    return JavaResolver(tmp_path, sources)


_USER = "package app;\npublic class User { public String who(){return \"w\";} }\n"


def test_typed_receiver_method_call_resolves(tmp_path: Path) -> None:
    # F-69: a receiver whose type is DECLARED (a param or a local `Type x`) resolves the instance
    # method call — sound, because the type is explicit (no inference).
    src = (
        "package app;\n"
        "public class Svc {\n"
        "  String run(User u) { return u.who(); }\n"
        "  String run2() { User v = new User(); return v.who(); }\n"
        "}\n"
    )
    resolver = _build_one(tmp_path, "app/Svc.java", src, ("app/User.java", _USER))
    svc = str((tmp_path / "app/Svc.java").resolve())
    # param-typed receiver: `u.who()` -> app.User.who
    r1 = resolver.resolve(svc, *_pos(src, "who", after="return u."))
    assert r1 is not None and r1.full_name == "app.User.who"
    # local-typed receiver: `v.who()` -> app.User.who
    r2 = resolver.resolve(svc, *_pos(src, "who", after="return v."))
    assert r2 is not None and r2.full_name == "app.User.who"


def test_external_typed_receiver_stays_unresolved(tmp_path: Path) -> None:
    # The receiver's declared type is external (java.util.List) — no in-repo target, so unresolved
    # (sound ceiling preserved). Also a method the type doesn't have must not resolve.
    src = (
        "package app;\n"
        "import java.util.List;\n"
        "public class Svc {\n"
        "  void a(List items) { items.size(); }\n"
        "  String b(User u) { return u.missing(); }\n"
        "}\n"
    )
    resolver = _build_one(tmp_path, "app/Svc.java", src, ("app/User.java", _USER))
    svc = str((tmp_path / "app/Svc.java").resolve())
    assert resolver.resolve(svc, *_pos(src, "size", after="items.")) is None  # external type
    assert resolver.resolve(svc, *_pos(src, "missing", after="u.")) is None  # not a method of User


def test_qualified_receiver_type_not_matched_via_same_named_import(tmp_path: Path) -> None:
    # Soundness (F-69 review): a receiver declared with a QUALIFIED type (`deep.Foo`) must not
    # resolve through a same-simple-name import (`other.Foo`) — that's a different class.
    svc = (
        "package app;\n"
        "import other.Foo;\n"
        "public class Svc {\n"
        "  void go(deep.Foo q) { q.run(); }\n"
        "}\n"
    )
    other_foo = "package other;\npublic class Foo { public void run(){} }\n"
    resolver = _build_one(tmp_path, "app/Svc.java", svc, ("other/Foo.java", other_foo))
    svc_abs = str((tmp_path / "app/Svc.java").resolve())
    assert resolver.resolve(svc_abs, *_pos(svc, "run", after="q.")) is None  # deep.Foo not in-repo


def test_field_receiver_not_overridden_by_a_later_local(tmp_path: Path) -> None:
    # Soundness (F-69 review): a field `Bar x` is the receiver at the call; a LATER local `Baz x`
    # must not override it (the before-position guard).
    svc = (
        "package app;\n"
        "public class Svc {\n"
        "  Bar x;\n"
        "  void go() { x.bar(); Baz x = new Baz(); }\n"
        "}\n"
    )
    bar = "package app;\npublic class Bar { public void bar(){} }\n"
    baz = "package app;\npublic class Baz { public void baz(){} }\n"
    resolver = _build_one(
        tmp_path, "app/Svc.java", svc, ("app/Bar.java", bar), ("app/Baz.java", baz)
    )
    svc_abs = str((tmp_path / "app/Svc.java").resolve())
    r = resolver.resolve(svc_abs, *_pos(svc, "bar", after="x."))
    assert r is not None and r.full_name == "app.Bar.bar"  # the field's type, not the later local


def test_same_class_call_resolves_to_method(tmp_path: Path) -> None:
    resolver, _ = _build(tmp_path)
    user = str((tmp_path / "app/models/User.java").resolve())
    line, col = _pos(_FILES["app/models/User.java"], "helper", after="return ")
    r = resolver.resolve(user, line, col)
    assert r is not None and r.def_type == "function" and r.full_name == "app.models.User.helper"


def test_instance_receiver_call_is_unresolved(tmp_path: Path) -> None:
    # items.size() — `items` is a parameter of unknown (external) type → no type inference → None.
    resolver, _ = _build(tmp_path)
    auth = str((tmp_path / "app/auth/Auth.java").resolve())
    line, col = _pos(_FILES["app/auth/Auth.java"], "size", after="items.")
    assert resolver.resolve(auth, line, col) is None
