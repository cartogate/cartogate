"""JavaScript resolver: JS-suffix path resolution + CommonJS require + inherited ceilings."""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.extract.resolver_ts import _resolve_module
from cartogate.schema.enums import EdgeType
from cartogate.store import InMemoryStore

_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")


def test_resolve_module_js_suffixes(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    files = {tmp_path / "a.js", tmp_path / "models.js", tmp_path / "View.jsx",
             tmp_path / "pkg" / "index.js"}
    for f in files:
        f.write_text("", encoding="utf-8")
    source_set = {str(f.resolve()) for f in files}
    importer = str((tmp_path / "a.js").resolve())

    assert _resolve_module(importer, "./models", source_set, _JS_SUFFIXES) == str(
        (tmp_path / "models.js").resolve()
    )
    assert _resolve_module(importer, "./View", source_set, _JS_SUFFIXES) == str(
        (tmp_path / "View.jsx").resolve()
    )
    assert _resolve_module(importer, "./pkg", source_set, _JS_SUFFIXES) == str(
        (tmp_path / "pkg" / "index.js").resolve()
    )
    assert _resolve_module(importer, "lodash", source_set, _JS_SUFFIXES) is None  # bare → external


def _call_dsts(tmp_path: Path) -> set[str]:
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id="p", store=store, base=tmp_path, index_docs=False)
    by_id = {n.id: n for n in result.nodes}
    return {by_id[e.dst].qualified_name for e in result.edges if e.type is EdgeType.CALLS}


def test_commonjs_require_destructure_resolves(tmp_path: Path) -> None:
    (tmp_path / "auth.js").write_text(
        "export function authenticate(name) { return name; }\n", encoding="utf-8"
    )
    (tmp_path / "service.cjs").write_text(
        'const { authenticate } = require("./auth");\n'
        "function login(name) { return authenticate(name); }\n",
        encoding="utf-8",
    )
    assert "auth.authenticate" in _call_dsts(tmp_path)  # require-bound name resolves to the symbol


def test_inferred_receiver_method_call_unresolved(tmp_path: Path) -> None:
    # Inherited ceiling: a method call on a value receiver needs type inference → no wrong edge.
    (tmp_path / "m.js").write_text(
        "export class Svc { run() {} }\n"
        "export function use(s) { s.run(); }\n",
        encoding="utf-8",
    )
    assert "m.Svc.run" not in _call_dsts(tmp_path)


def test_local_shadow_unresolved(tmp_path: Path) -> None:
    # Inherited shadow guard: a local that shadows a top-level fn must not resolve to it.
    (tmp_path / "m.js").write_text(
        "export function format(s) { return s; }\n"
        "export function process(data) {\n"
        "  const format = (s) => s.trim();\n"
        "  return format(data);\n"
        "}\n",
        encoding="utf-8",
    )
    assert "m.format" not in _call_dsts(tmp_path)
