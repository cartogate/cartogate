"""Unit tests for the pure-Python C# name resolver (namespace + using + receiver rules)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_csharp import CSharpResolver

MODELS = """namespace App.Models
{
    public class Base { public void Init() { } }

    public class User : Base
    {
        public User(string name) { }
        public bool IsActive() { return true; }
    }
}
"""

SERVICES = """using App.Models;

namespace App.Services
{
    public class AuthService
    {
        private User _user;

        public AuthService(User u) { _user = u; }

        public bool Authenticate()
        {
            return Validate() && _user.IsActive();
        }

        private bool Validate() { return true; }

        public static User Make() { return new User("a"); }
    }
}
"""


def _resolver(tmp_path: Path) -> tuple[CSharpResolver, dict[str, str], dict[str, str]]:
    models = tmp_path / "Models.cs"
    services = tmp_path / "Services.cs"
    models.write_text(MODELS, encoding="utf-8")
    services.write_text(SERVICES, encoding="utf-8")
    srcs = {str(models): MODELS, str(services): SERVICES}
    texts = {str(models): MODELS, str(services): SERVICES}
    return CSharpResolver(tmp_path, srcs), {"models": str(models), "services": str(services)}, texts


def _loc(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(needle, start)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    return line, col


def test_new_resolves_across_namespace_via_using(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(SERVICES, "User", SERVICES.index("new User") + 4)
    hit = r.resolve(paths["services"], line, col)
    assert hit is not None and hit.full_name == "App.Models.User" and hit.def_type == "class"


def test_base_type_resolves(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(MODELS, "Base", MODELS.index(": Base") + 2)
    hit = r.resolve(paths["models"], line, col)
    assert hit is not None and hit.full_name == "App.Models.Base"


def test_same_class_unqualified_call_resolves(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(SERVICES, "Validate()", SERVICES.index("return Validate"))
    hit = r.resolve(paths["services"], line, col)
    assert hit is not None and hit.full_name == "App.Services.AuthService.Validate"


def test_declared_receiver_method_call_resolves(tmp_path: Path) -> None:
    # `_user` is a field of declared type `User`, so `_user.IsActive()` binds (declared receiver).
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(SERVICES, "IsActive")
    hit = r.resolve(paths["services"], line, col)
    assert hit is not None and hit.full_name == "App.Models.User.IsActive"


def test_plain_namespace_using_is_unresolved(tmp_path: Path) -> None:
    # `using App.Models;` is a namespace, not a single definition -> no resolution (external dep).
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(SERVICES, "Models", SERVICES.index("using App.Models") + len("using App."))
    assert r.resolve(paths["services"], line, col) is None


def test_inferred_receiver_is_unresolved(tmp_path: Path) -> None:
    # `var s = Make(); s.IsActive();` — `s`'s type is inferred, so the receiver call is unresolved.
    src = (
        "using App.Models;\n"
        "namespace App.Services {\n"
        "  public class C {\n"
        "    public void M() { var s = Make(); s.IsActive(); }\n"
        "    public static User Make() { return new User(\"a\"); }\n"
        "  }\n"
        "}\n"
    )
    models = tmp_path / "Models.cs"
    c = tmp_path / "C.cs"
    models.write_text(MODELS, encoding="utf-8")
    c.write_text(src, encoding="utf-8")
    r = CSharpResolver(tmp_path, {str(models): MODELS, str(c): src})
    line, col = _loc(src, "IsActive")
    assert r.resolve(str(c), line, col) is None  # sound ceiling: no inference


def test_alias_using_resolves_to_type(tmp_path: Path) -> None:
    src = (
        "using Person = App.Models.User;\n"
        "namespace App.Services {\n"
        "  public class C { public Person Get() { return null; } }\n"
        "}\n"
    )
    models = tmp_path / "Models.cs"
    c = tmp_path / "C.cs"
    models.write_text(MODELS, encoding="utf-8")
    c.write_text(src, encoding="utf-8")
    r = CSharpResolver(tmp_path, {str(models): MODELS, str(c): src})
    line, col = _loc(src, "Person", src.index("public Person"))
    hit = r.resolve(str(c), line, col)
    assert hit is not None and hit.full_name == "App.Models.User"
