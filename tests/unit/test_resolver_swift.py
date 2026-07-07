"""Unit tests for the pure-Python Swift name resolver (global names + receiver rules)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_swift import SwiftResolver

MODELS = """class Base {
    func initialize() {}
}

class User: Base {
    let name: String
    init(name: String) { self.name = name }
    func isActive() -> Bool { return name.isEmpty == false }
}

extension User {
    func greet() -> String { return name }
}
"""

SERVICE = """func validate(name: String) -> Bool { return name.isEmpty == false }

class AuthService {
    func authenticate(u: User) -> Bool {
        return validate(name: "admin") && u.isActive()
    }

    func make() -> User { return User(name: "admin") }
}
"""


def _resolver(tmp_path: Path) -> tuple[SwiftResolver, dict[str, str]]:
    (tmp_path / "Models.swift").write_text(MODELS, encoding="utf-8")
    (tmp_path / "Service.swift").write_text(SERVICE, encoding="utf-8")
    srcs = {str(tmp_path / "Models.swift"): MODELS, str(tmp_path / "Service.swift"): SERVICE}
    return SwiftResolver(tmp_path, srcs), {"models": str(tmp_path / "Models.swift"),
                                           "service": str(tmp_path / "Service.swift")}


def _loc(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(needle, start)
    return text.count("\n", 0, idx) + 1, idx - (text.rfind("\n", 0, idx) + 1)


def test_initializer_call_resolves_to_class(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "User", SERVICE.index("return User") + len("return "))
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "User" and hit.def_type == "class"


def test_cross_file_type_resolves_without_import(tmp_path: Path) -> None:
    # Swift's flat module namespace: `User` in Service.swift binds to Models.swift, no import.
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "User", SERVICE.index("u: User") + len("u: "))
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "User" and hit.def_path.name == "Models.swift"


def test_declared_receiver_method_call_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "isActive")
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "User.isActive"


def test_top_level_function_call_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "validate", SERVICE.index("return validate"))
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "validate" and hit.def_type == "function"


def test_extension_method_is_indexed(tmp_path: Path) -> None:
    # A method added by `extension User { func greet() }` resolves via a declared receiver.
    src = (
        "func use(u: User) -> String { return u.greet() }\n"
    )
    (tmp_path / "Models.swift").write_text(MODELS, encoding="utf-8")
    extra = tmp_path / "Use.swift"
    extra.write_text(src, encoding="utf-8")
    r = SwiftResolver(tmp_path, {str(tmp_path / "Models.swift"): MODELS, str(extra): src})
    line, col = _loc(src, "greet")
    hit = r.resolve(str(extra), line, col)
    assert hit is not None and hit.full_name == "User.greet"


def test_supertype_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(MODELS, "Base", MODELS.index(": Base") + 2)
    hit = r.resolve(paths["models"], line, col)
    assert hit is not None and hit.full_name == "Base"


def test_inferred_receiver_is_unresolved(tmp_path: Path) -> None:
    # `let u = make(); u.isActive()` — `u`'s type is inferred -> the receiver call is unresolved.
    src = (
        "class C {\n"
        "    func make() -> User { return User(name: \"a\") }\n"
        "    func f() -> Bool { let u = make(); return u.isActive() }\n"
        "}\n"
    )
    (tmp_path / "Models.swift").write_text(MODELS, encoding="utf-8")
    extra = tmp_path / "C.swift"
    extra.write_text(src, encoding="utf-8")
    r = SwiftResolver(tmp_path, {str(tmp_path / "Models.swift"): MODELS, str(extra): src})
    line, col = _loc(src, "isActive")
    assert r.resolve(str(extra), line, col) is None
