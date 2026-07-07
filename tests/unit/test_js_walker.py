"""Structural facts the JavaScript walker emits (no resolution) — incl. JSX + arrow consts."""

from __future__ import annotations

from cartogate.extract.ast_walker import (
    NAME_CALL,
    NAME_IMPORT,
    NAME_INHERIT,
    NAME_REFERENCE,
)
from cartogate.extract.js_walker import JavaScriptWalker
from cartogate.schema.enums import Visibility

_SOURCE = """import { User } from "./models";

export function authenticate(name) {
  return validate(name) && new User(name);
}

function validate(name) {
  return name.length > 0;
}

export const App = (props) => {
  const u = authenticate(props.name);
  return <User name={u}><div>hi</div></User>;
};

class Service extends Base {
  run() {
    helper();
  }
}
"""


def _walk():
    return JavaScriptWalker().walk(
        _SOURCE.encode("utf-8"), module_qname="m", rel_path="m.jsx", abs_path="/x/m.jsx",
    )


def test_extracts_functions_arrow_consts_classes_methods() -> None:
    syms = {s.qualified_name for s in _walk().symbols}
    assert "m.authenticate" in syms  # function declaration
    assert "m.validate" in syms
    assert "m.App" in syms  # exported arrow const
    assert "m.Service" in syms  # class
    assert "m.Service.run" in syms  # method → container is the class


def test_container_qnames() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["m.authenticate"].container_qname == "m"  # top-level
    assert by_name["m.Service.run"].container_qname == "m.Service"  # method → the class


def test_visibility_by_export() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["m.authenticate"].visibility is Visibility.EXPORTED  # `export function`
    assert by_name["m.App"].visibility is Visibility.EXPORTED  # `export const`
    assert by_name["m.validate"].visibility is Visibility.INTERNAL  # no export


def test_signatures() -> None:
    by_name = {s.qualified_name: s for s in _walk().symbols}
    assert by_name["m.authenticate"].signature == "authenticate(name)"
    assert by_name["m.App"].signature == "App(props)"  # arrow-const params kept
    assert by_name["m.Service"].signature == "Service(Base)"  # class with its base


def test_name_occurrences_including_jsx() -> None:
    relations = {(n.relation, n.text) for n in _walk().names}
    assert (NAME_IMPORT, "User") in relations  # ESM import
    assert (NAME_CALL, "validate") in relations  # same-file call
    assert (NAME_CALL, "User") in relations  # new User(...)
    assert (NAME_INHERIT, "Base") in relations  # extends Base
    assert (NAME_REFERENCE, "User") in relations  # <User/> JSX component reference


def test_lowercase_jsx_tag_is_not_a_reference() -> None:
    # `<div>` is an HTML element, not a component — it must NOT become a reference.
    refs = {n.text for n in _walk().names if n.relation == NAME_REFERENCE}
    assert "div" not in refs
