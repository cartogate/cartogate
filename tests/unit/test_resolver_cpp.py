"""Unit tests for the pure-Python C++ name resolver (types, methods, qualified/receiver calls)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_cpp import CppResolver

HPP = """#pragma once
namespace app {
class Base { public: void init(); };
class User : public Base {
    std::string name_;
public:
    User(const std::string &name);
    bool isActive() const;
};
User *makeUser(const std::string &name);
}
"""

CPP = """#include "user.hpp"
namespace app {
static bool validate(const std::string &n) { return !n.empty(); }
bool User::isActive() const { return validate(name_); }
User *makeUser(const std::string &name) { return new User(name); }
void caller() {
    User u("a");
    u.isActive();
    User *p = makeUser("b");
    p->isActive();
    app::makeUser("c");
}
}
"""


def _resolver(tmp_path: Path) -> tuple[CppResolver, dict[str, str]]:
    (tmp_path / "user.hpp").write_text(HPP, encoding="utf-8")
    (tmp_path / "user.cpp").write_text(CPP, encoding="utf-8")
    srcs = {str(tmp_path / "user.hpp"): HPP, str(tmp_path / "user.cpp"): CPP}
    return CppResolver(tmp_path, srcs), {"hpp": str(tmp_path / "user.hpp"),
                                         "cpp": str(tmp_path / "user.cpp")}


def _loc(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(needle, start)
    return text.count("\n", 0, idx) + 1, idx - (text.rfind("\n", 0, idx) + 1)


def test_new_resolves_to_class(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "User", CPP.index("new User") + 4)
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.full_name == "User" and hit.def_type == "class"


def test_base_type_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(HPP, "Base", HPP.index(": public Base") + len(": public "))
    hit = r.resolve(paths["hpp"], line, col)
    assert hit is not None and hit.full_name == "Base"


def test_out_of_line_method_is_indexed_and_receiver_call_resolves(tmp_path: Path) -> None:
    # `User u; u.isActive()` -> the out-of-line `User::isActive` definition (declared receiver).
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "isActive", CPP.index("u.isActive"))
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.full_name == "User::isActive" and hit.def_type == "function"


def test_pointer_receiver_call_resolves(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "isActive", CPP.index("p->isActive"))
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.full_name == "User::isActive"


def test_unqualified_call_in_method_resolves_free_function(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "validate(name_)", 0)
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.full_name == "validate"


def test_qualified_namespace_call_resolves_free_function(tmp_path: Path) -> None:
    # `app::makeUser(...)` -> the free function (the `app` scope is a namespace, not a class).
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "makeUser", CPP.index("app::makeUser") + len("app::"))
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.full_name == "makeUser"


def test_relative_include_resolves_to_header_module(tmp_path: Path) -> None:
    r, paths = _resolver(tmp_path)
    line, col = _loc(CPP, "user.hpp")
    hit = r.resolve(paths["cpp"], line, col)
    assert hit is not None and hit.def_type == "module" and hit.def_path.name == "user.hpp"


def test_inferred_receiver_is_unresolved(tmp_path: Path) -> None:
    # `auto v = makeUser(); v->isActive();` — `v`'s type is inferred -> the call stays unresolved.
    src = (
        '#include "user.hpp"\n'
        "namespace app {\n"
        "void f() { auto v = makeUser(\"x\"); v->isActive(); }\n"
        "}\n"
    )
    (tmp_path / "user.hpp").write_text(HPP, encoding="utf-8")
    extra = tmp_path / "z.cpp"
    extra.write_text(src, encoding="utf-8")
    r = CppResolver(tmp_path, {str(tmp_path / "user.hpp"): HPP, str(extra): src})
    line, col = _loc(src, "isActive")
    assert r.resolve(str(extra), line, col) is None
