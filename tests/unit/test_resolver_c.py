"""Unit tests for the pure-Python C name resolver (global functions, statics, includes, types)."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.resolver_c import CResolver

USER_H = "struct User { char *name; };\nstruct User *create_user(const char *name);\n"
USER_C = (
    '#include "user.h"\n'
    "struct User *create_user(const char *name) { return 0; }\n"
)
AUTH_C = (
    '#include "user.h"\n'
    "static int validate(const char *name) { return name != 0; }\n"
    "int authenticate(const char *name) {\n"
    "    if (validate(name)) { struct User *u = create_user(name); return u != 0; }\n"
    "    return 0;\n"
    "}\n"
)


def _resolver(tmp_path: Path) -> tuple[CResolver, dict[str, str], dict[str, str]]:
    files = {"user.h": USER_H, "user.c": USER_C, "auth.c": AUTH_C}
    abspaths = {}
    srcs = {}
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        abspaths[name] = str(p)
        srcs[str(p)] = text
    return CResolver(tmp_path, srcs), abspaths, files


def _loc(text: str, needle: str, start: int = 0) -> tuple[int, int]:
    idx = text.index(needle, start)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    return line, col


def test_cross_file_call_resolves_via_global_index(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(AUTH_C, "create_user", AUTH_C.index("u = create_user") + 4)
    hit = r.resolve(paths["auth.c"], line, col)
    assert hit is not None and hit.full_name == "create_user" and hit.def_type == "function"
    assert hit.def_path.name == "user.c"  # the definition, not the header prototype


def test_same_file_static_call_resolves(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(AUTH_C, "validate(name)", AUTH_C.index("if (validate"))
    hit = r.resolve(paths["auth.c"], line, col)
    assert hit is not None and hit.full_name == "validate" and hit.def_path.name == "auth.c"


def test_static_is_not_visible_cross_file(tmp_path: Path) -> None:
    # `validate` is static in auth.c; a call from another file must NOT resolve (file-local linkage)
    extra = tmp_path / "other.c"
    extra.write_text('int caller(void) { return validate("x"); }\n', encoding="utf-8")
    files = {
        str(tmp_path / "user.h"): USER_H,
        str(tmp_path / "auth.c"): AUTH_C,
        str(extra): extra.read_text(),
    }
    (tmp_path / "user.h").write_text(USER_H, encoding="utf-8")
    (tmp_path / "auth.c").write_text(AUTH_C, encoding="utf-8")
    r = CResolver(tmp_path, files)
    text = extra.read_text()
    line, col = _loc(text, "validate")
    assert r.resolve(str(extra), line, col) is None


def test_relative_include_resolves_to_header_module(tmp_path: Path) -> None:
    r, paths, files = _resolver(tmp_path)
    line, col = _loc(files["auth.c"], "user.h")
    hit = r.resolve(paths["auth.c"], line, col)
    assert hit is not None and hit.def_type == "module" and hit.def_path.name == "user.h"


def test_struct_type_reference_resolves(tmp_path: Path) -> None:
    r, paths, _ = _resolver(tmp_path)
    line, col = _loc(AUTH_C, "User", AUTH_C.index("struct User *u") + len("struct "))
    hit = r.resolve(paths["auth.c"], line, col)
    assert hit is not None and hit.full_name == "User" and hit.def_type == "class"


def test_unknown_call_is_unresolved(tmp_path: Path) -> None:
    # A libc call (no in-repo definition) stays unresolved — no wrong edge.
    r, paths, _ = _resolver(tmp_path)
    extra = tmp_path / "z.c"
    extra.write_text('int f(void) { return printf("hi"); }\n', encoding="utf-8")
    r2 = CResolver(tmp_path, {str(extra): extra.read_text()})
    text = extra.read_text()
    line, col = _loc(text, "printf")
    assert r2.resolve(str(extra), line, col) is None
