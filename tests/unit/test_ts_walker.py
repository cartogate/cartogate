"""TypeScript tree-sitter walker → raw structural facts."""

from __future__ import annotations

from cartogate.extract.ast_walker import FileFacts
from cartogate.extract.ts_walker import TypeScriptWalker
from cartogate.schema.enums import Visibility

_SOURCE = """
export function run(a: number, b: string): void {}
function helper(): void {}
export const arrow = (x: string): string => x;

export class Service extends Base {
  private secret: number = 1;
  go(n: number): void {}
}

export interface Repo<T> {
  find(id: string): T;
}
"""


def _walk(source: str) -> FileFacts:
    return TypeScriptWalker().walk(
        source.encode("utf-8"), module_qname="pkg.svc", rel_path="pkg/svc.ts", abs_path="/x/svc.ts"
    )


def test_extracts_functions_classes_methods_interfaces() -> None:
    syms = {s.qualified_name: s for s in _walk(_SOURCE).symbols}
    assert set(syms) == {
        "pkg.svc.run",
        "pkg.svc.helper",
        "pkg.svc.arrow",
        "pkg.svc.Service",
        "pkg.svc.Service.go",
        "pkg.svc.Repo",
        "pkg.svc.Repo.find",
    }


def test_container_qnames_distinguish_top_level_from_members() -> None:
    syms = {s.qualified_name: s for s in _walk(_SOURCE).symbols}
    assert syms["pkg.svc.run"].container_qname == "pkg.svc"  # top-level (module is the container)
    assert syms["pkg.svc.arrow"].container_qname == "pkg.svc"  # arrow const, top-level
    assert syms["pkg.svc.Service.go"].container_qname == "pkg.svc.Service"  # method


def test_visibility_from_export_and_access_modifiers() -> None:
    syms = {s.qualified_name: s for s in _walk(_SOURCE).symbols}
    assert syms["pkg.svc.run"].visibility is Visibility.EXPORTED  # `export`
    assert syms["pkg.svc.helper"].visibility is Visibility.INTERNAL  # not exported
    assert syms["pkg.svc.Service.go"].visibility is Visibility.PUBLIC  # method, no modifier


def test_signature_keeps_param_text_for_later_normalization() -> None:
    syms = {s.qualified_name: s for s in _walk(_SOURCE).symbols}
    assert syms["pkg.svc.run"].signature == "run(a: number, b: string)"


def test_emits_name_occurrences_for_resolution() -> None:
    src = (
        'import { User } from "./models";\n'
        "export function run(): User {\n"
        "  return make();\n"
        "}\n"
        "function make(): User { return new User(); }\n"
        "export class Admin extends Base {}\n"
    )
    names = _walk(src).names
    by_rel: dict[str, set[str]] = {}
    for n in names:
        by_rel.setdefault(n.relation, set()).add(n.text)
    assert "User" in by_rel["import"]  # imported name (module specifier carried separately)
    assert "make" in by_rel["call"]  # a call inside a function body
    assert "User" in by_rel["call"]  # `new User()` is a construction call
    assert "Base" in by_rel["inherit"]  # extends base
    # the import RawName carries its source specifier for external-package labelling
    assert any(n.relation == "import" and n.module == "./models" for n in names)


def test_class_signature_includes_bases() -> None:
    # Bases disambiguate classes for the gate, like the Python walker (Foo(Bar) vs Foo()).
    syms = {s.qualified_name: s for s in _walk(_SOURCE).symbols}
    assert syms["pkg.svc.Service"].signature == "Service(Base)"  # extends Base
    extra = _walk("export class Repo<T> extends Store<T> implements I {}").symbols
    assert next(s for s in extra if s.name == "Repo").signature == "Repo(Store,I)"
    assert next(s for s in _walk("export class Plain {}").symbols).signature == "Plain()"
