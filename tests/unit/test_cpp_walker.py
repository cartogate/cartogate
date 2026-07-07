"""Unit tests for the C++ structural walker (classes, methods, namespaces, calls, includes)."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
)
from cartogate.extract.cpp_walker import CppWalker

HPP = b"""#include <string>
namespace app {
class Base { public: virtual void init(); };
class User : public Base {
    std::string name_;
public:
    User(const std::string &name);
    bool isActive() const;
    void run() { isActive(); }
};
}
"""

CPP = b"""#include "user.hpp"
namespace app {
bool User::isActive() const { return validate(name_); }
User *makeUser(const std::string &name) { return new User(name); }
}
"""


def _walk(src: bytes, module: str):
    return CppWalker().walk(
        src, module_qname=module, rel_path=f"{module}.x", abs_path=f"/x/{module}"
    )


def test_classes_and_inline_method() -> None:
    by = {s.qualified_name: s for s in _walk(HPP, "user").symbols}
    assert "user.Base" in by and "user.User" in by
    # An inline method definition is a symbol; the bare declarations (User/isActive) are not.
    assert "user.User.run" in by
    assert "user.User.isActive" not in by  # declared in-class (no body) -> not a symbol here
    assert by["user.User.run"].container_qname == "user.User"


def test_out_of_line_method_and_free_function() -> None:
    by = {s.qualified_name: s for s in _walk(CPP, "user").symbols}
    # `bool User::isActive()` out-of-line definition -> a method under the class.
    assert "user.User.isActive" in by and by["user.User.isActive"].container_qname == "user.User"
    # A free function is module-level.
    assert "user.makeUser" in by and by["user.makeUser"].container_qname == "user"


def test_inherit_call_and_include_occurrences() -> None:
    hpp = {(n.relation, n.text) for n in _walk(HPP, "user").names}
    assert (NAME_INHERIT, "Base") in hpp
    assert (NAME_IMPORT, "string") in hpp  # <string> -> external
    assert (NAME_CALL, "isActive") in hpp  # inline run() calls isActive
    cpp = {(n.relation, n.text) for n in _walk(CPP, "user").names}
    assert (NAME_IMPORT, "user.hpp") in cpp  # relative include
    assert (NAME_CALL, "validate") in cpp  # unqualified call in the out-of-line method
    assert (NAME_CALL, "User") in cpp  # `new User(...)` -> the constructor's type


def test_static_free_function_is_internal() -> None:
    src = b'namespace n { static int helper() { return 0; } int pub() { return 1; } }'
    by = {s.qualified_name: s for s in _walk(src, "m").symbols}
    from cartogate.schema.enums import Visibility
    assert by["m.helper"].visibility is Visibility.INTERNAL
    assert by["m.pub"].visibility is Visibility.EXPORTED
