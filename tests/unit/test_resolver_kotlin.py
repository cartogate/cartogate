"""Unit tests for the pure-Python Kotlin name resolver (package + import + receiver rules)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_kotlin import KotlinResolver

MODELS = """package app.models

open class Base {
    open fun init() {}
}

class User(val name: String) : Base() {
    fun isActive(): Boolean {
        return name.isNotEmpty()
    }
}
"""

SERVICE = """package app.service

import app.models.User

fun validate(name: String): Boolean {
    return name.isNotEmpty()
}

class AuthService {
    fun authenticate(u: User): Boolean {
        return validate("admin") && u.isActive()
    }

    fun make(): User {
        return User("admin")
    }
}
"""


def _resolver(tmp_path: Path) -> tuple[KotlinResolver, dict[str, str]]:
    (tmp_path / "models.kt").write_text(MODELS, encoding="utf-8")
    (tmp_path / "service.kt").write_text(SERVICE, encoding="utf-8")
    srcs = {str(tmp_path / "models.kt"): MODELS, str(tmp_path / "service.kt"): SERVICE}
    return KotlinResolver(tmp_path, srcs), {"models": str(tmp_path / "models.kt"),
                                            "service": str(tmp_path / "service.kt")}


def _loc(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(needle, start)
    return text.count("\n", 0, idx) + 1, idx - (text.rfind("\n", 0, idx) + 1)


def test_constructor_call_resolves_to_class(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "User", SERVICE.index("return User") + len("return "))
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "app.models.User" and hit.def_type == "class"


def test_cross_package_import_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    after_import = SERVICE.index("import app.models.") + len("import app.models.")
    line, col = _loc(SERVICE, "User", after_import)
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "app.models.User"


def test_declared_receiver_method_call_resolves(tmp_path: Path) -> None:
    # `u: User` parameter -> `u.isActive()` binds (declared, not inferred).
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "isActive")
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "app.models.User.isActive"


def test_top_level_function_call_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(SERVICE, "validate(\"admin\")")
    hit = r.resolve(paths["service"], line, col)
    assert hit is not None and hit.full_name == "app.service.validate"
    assert hit.def_type == "function"


def test_supertype_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(MODELS, "Base", MODELS.index(": Base") + 2)
    hit = r.resolve(paths["models"], line, col)
    assert hit is not None and hit.full_name == "app.models.Base"


def test_inferred_receiver_is_unresolved(tmp_path: Path) -> None:
    # `val u = make(); u.isActive()` — `u`'s type is inferred -> the receiver call is unresolved.
    src = (
        "package app.service\n"
        "import app.models.User\n"
        "fun f(): Boolean {\n"
        "    val u = User(\"a\")\n"
        "    return u.isActive()\n"
        "}\n"
    )
    (tmp_path / "models.kt").write_text(MODELS, encoding="utf-8")
    extra = tmp_path / "z.kt"
    extra.write_text(src, encoding="utf-8")
    r = KotlinResolver(tmp_path, {str(tmp_path / "models.kt"): MODELS, str(extra): src})
    line, col = _loc(src, "isActive")
    assert r.resolve(str(extra), line, col) is None
