"""Structural facts the Go walker emits (no resolution)."""

from __future__ import annotations

from cartogate.extract.ast_walker import NAME_CALL, NAME_IMPORT, NAME_INHERIT
from cartogate.extract.go_walker import GoWalker
from cartogate.schema.enums import Visibility

_SOURCE = """package models

import "fmt"

type Base struct {
	Name string
}

type User struct {
	Base
	age int
}

type Greeter interface {
	Greet() string
}

func NewUser(name string) *User {
	fmt.Println(name)
	return &User{}
}

func (u *User) Greet() string { return u.Name }

func (u *User) secret() int { return u.age }
"""


def _walk():
    return GoWalker().walk(
        _SOURCE.encode("utf-8"), module_qname="models", rel_path="models/m.go",
        abs_path="/x/models/m.go",
    )


def test_extracts_funcs_types_and_methods() -> None:
    syms = {s.qualified_name for s in _walk().symbols}
    assert "models.NewUser" in syms  # free function
    assert {"models.Base", "models.User", "models.Greeter"} <= syms  # types
    assert "models.User.Greet" in syms  # method → container is the receiver type
    assert "models.User.secret" in syms


def test_container_qnames_and_top_level() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["models.NewUser"].container_qname == "models"  # func → package (top-level)
    assert by_name["models.User"].container_qname == "models"  # type → package (top-level)
    assert by_name["models.User.Greet"].container_qname == "models.User"  # method → receiver type


def test_visibility_by_capitalization() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["models.NewUser"].visibility is Visibility.PUBLIC  # exported
    assert by_name["models.User.secret"].visibility is Visibility.INTERNAL  # unexported


def test_signatures() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["models.User"].signature == "User"  # type → bare name
    assert by_name["models.NewUser"].signature == "NewUser(name string)"  # func keeps param text
    assert by_name["models.User.Greet"].signature == "Greet()"  # receiver dropped


def test_name_occurrences() -> None:
    relations = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_IMPORT, "fmt") in relations
    assert (NAME_INHERIT, "Base") in relations  # struct embedding
    assert (NAME_CALL, "Println") in relations  # fmt.Println → call at the selector field
