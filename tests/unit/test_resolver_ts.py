"""Unit tests for the pure-Python TypeScript resolver — path resolution + the ceiling."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.extract.resolver_ts import _resolve_module
from cartogate.schema.enums import EdgeType
from cartogate.store import InMemoryStore


def test_resolve_module_relative_and_bare(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    files = {tmp_path / "a.ts", tmp_path / "models.ts", tmp_path / "pkg" / "index.ts"}
    for f in files:
        f.write_text("", encoding="utf-8")
    source_set = {str(f.resolve()) for f in files}
    importer = str((tmp_path / "a.ts").resolve())

    models = str((tmp_path / "models.ts").resolve())
    assert _resolve_module(importer, "./models", source_set) == models
    # a directory import resolves to its index file
    assert _resolve_module(importer, "./pkg", source_set) == str(
        (tmp_path / "pkg" / "index.ts").resolve()
    )
    # bare specifiers (packages) are external -> not a local file
    assert _resolve_module(importer, "lodash", source_set) is None
    assert _resolve_module(importer, "@scope/x", source_set) is None


def _calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    proj = tmp_path / "proj"
    proj.mkdir()
    for name, body in files.items():
        (proj / name).write_text(body, encoding="utf-8")
    store = InMemoryStore()
    result = index_package(proj, repo_id="proj", store=store, index_docs=False)
    by_id = {n.id: n for n in result.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in result.edges
        if e.type is EdgeType.CALLS
    }


def test_typed_receiver_method_call_resolves(tmp_path: Path) -> None:
    # F-69: a method call on a receiver with an explicitly DECLARED type resolves — a `s: Svc`
    # annotation and a `const c = new Svc()`. An inferred receiver (`const d = make()`) stays
    # unresolved (sound: a function's return type isn't read).
    calls = _calls(
        tmp_path,
        {
            "m.ts": (
                "export class Svc {\n  run(): void {}\n}\n"
                "export function make(): Svc { return new Svc(); }\n"
                "export function use(s: Svc): void {\n  s.run();\n}\n"
                "export function use2(): void {\n  const c = new Svc(); c.run();\n}\n"
                "export function use3(): void {\n  const d = make(); d.run();\n}\n"
            )
        },
    )
    assert ("proj.m.use", "proj.m.Svc.run") in calls  # param annotation
    assert ("proj.m.use2", "proj.m.Svc.run") in calls  # const c = new Svc()
    assert ("proj.m.use3", "proj.m.Svc.run") not in calls  # inferred from make() — unresolved


def test_imported_typed_receiver_method_resolves(tmp_path: Path) -> None:
    # The receiver's class is imported from another file — resolves across the import (sound).
    calls = _calls(
        tmp_path,
        {
            "svc.ts": "export class Svc {\n  run(): void {}\n}\n",
            "app.ts": (
                "import { Svc } from './svc';\n"
                "export function use(s: Svc): void {\n  s.run();\n}\n"
            ),
        },
    )
    assert ("proj.app.use", "proj.svc.Svc.run") in calls


def test_same_named_local_classes_do_not_cross_resolve(tmp_path: Path) -> None:
    # Soundness (review MEDIUM): two `class Local` in different functions are DISTINCT types sharing
    # a bare name. The member index is top-level only, so a typed receiver of a function-local class
    # resolves to neither (no cross-function mis-attribution) rather than the wrong one.
    calls = _calls(
        tmp_path,
        {
            "m.ts": (
                "export function a(): number {\n"
                "  class Local { m(): number { return 1; } }\n"
                "  const x: Local = new Local(); return x.m();\n"
                "}\n"
                "export function b(): number {\n"
                "  class Local { m(): number { return 2; } }\n"
                "  const y: Local = new Local(); return y.m();\n"
                "}\n"
            )
        },
    )
    assert not any(dst.endswith("Local.m") for _, dst in calls)  # neither resolves (sound)


def test_getter_is_not_resolved_as_a_method(tmp_path: Path) -> None:
    # Soundness (review LOW): `get foo()` is a property accessor, not a callable method. A `foo()`
    # call must not be attributed to the getter definition.
    calls = _calls(
        tmp_path,
        {
            "m.ts": (
                "export class Svc {\n  get foo(): () => void { return () => {}; }\n}\n"
                "export function use(s: Svc): void {\n  s.foo();\n}\n"
            )
        },
    )
    assert not any(dst.endswith("Svc.foo") for _, dst in calls)  # getter not indexed as a method


def test_local_shadow_does_not_resolve_to_top_level(tmp_path: Path) -> None:
    # Soundness: a local that shadows a top-level symbol of the same name must NOT resolve to it.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "m.ts").write_text(
        "export function format(s: string): string {\n  return s;\n}\n"
        "export function process(data: string): string {\n"
        "  const format = (s: string) => s.trim();\n"
        "  return format(data);\n"
        "}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(proj, repo_id="proj", store=store, index_docs=False)
    by_id = {n.id: n for n in result.nodes}
    to_top_format = [
        e
        for e in result.edges
        if e.type in (EdgeType.CALLS, EdgeType.REFERENCES)
        and by_id[e.dst].qualified_name == "proj.m.format"
    ]
    assert to_top_format == []  # the local `format` is not the top-level `format`
