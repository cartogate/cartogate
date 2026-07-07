"""Section 2 gate — structural extraction against a known fixture package.

Indexes ``tests/fixtures/sample_pkg`` and asserts the exact node set and the exact set
of each v0 structural edge type, all tagged ``EXTRACTED``. Name resolution is performed
by jedi (the engine pylsp wraps), in-process and deterministic. Also asserts a SCIP
artifact is emitted and parses, and that resolution emits a span.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.extract.scip_emit import emit_scip
from cartogate.instrument import Phase, SpanRecorder
from cartogate.schema.enums import Confidence, EdgeType, Language, NodeKind
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"
REPO = "test-repo"


@pytest.fixture(scope="module")
def indexed() -> object:
    store = InMemoryStore()
    recorder = SpanRecorder(rss_sampler=lambda: 1)
    result = index_package(FIXTURE_ROOT, repo_id=REPO, store=store, recorder=recorder)
    return _Indexed(result=result, store=store, recorder=recorder)


class _Indexed:
    def __init__(self, *, result: object, store: InMemoryStore, recorder: SpanRecorder) -> None:
        self.result = result
        self.store = store
        self.recorder = recorder
        self.by_id = {n.id: n for n in result.nodes}  # type: ignore[attr-defined]

    def qnames(self, kind: NodeKind | None = None) -> set[str]:
        return {
            n.qualified_name
            for n in self.by_id.values()
            if kind is None or n.kind is kind
        }

    def edges_of(self, edge_type: EdgeType) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for e in self.result.edges:  # type: ignore[attr-defined]
            if e.type is edge_type:
                out.add((self.by_id[e.src].qualified_name, self.by_id[e.dst].qualified_name))
        return out


def test_module_and_symbol_nodes(indexed: _Indexed) -> None:
    symbols = indexed.qnames(NodeKind.SYMBOL)
    assert symbols == {
        "sample_pkg.models.Base",
        "sample_pkg.models.Base.greet",
        "sample_pkg.models.User",
        "sample_pkg.models.User.__init__",
        "sample_pkg.auth.authenticate",
        "sample_pkg.auth.validate",
        "sample_pkg.auth.make_user",
        "sample_pkg.auth.pid",
    }
    modules = indexed.qnames(NodeKind.MODULE)
    assert {"sample_pkg.models", "sample_pkg.auth"} <= modules
    assert "os" in indexed.qnames(NodeKind.EXTERNAL_PACKAGE)


def test_defines_edges(indexed: _Indexed) -> None:
    assert indexed.edges_of(EdgeType.DEFINES) == {
        ("sample_pkg.models", "sample_pkg.models.Base"),
        ("sample_pkg.models", "sample_pkg.models.User"),
        ("sample_pkg.models.Base", "sample_pkg.models.Base.greet"),
        ("sample_pkg.models.User", "sample_pkg.models.User.__init__"),
        ("sample_pkg.auth", "sample_pkg.auth.authenticate"),
        ("sample_pkg.auth", "sample_pkg.auth.validate"),
        ("sample_pkg.auth", "sample_pkg.auth.make_user"),
        ("sample_pkg.auth", "sample_pkg.auth.pid"),
    }


def test_inherits_edges(indexed: _Indexed) -> None:
    assert indexed.edges_of(EdgeType.INHERITS) == {
        ("sample_pkg.models.User", "sample_pkg.models.Base"),
    }


def test_calls_edges(indexed: _Indexed) -> None:
    # Intra-file call + cross-file class instantiation (call resolves to the class).
    assert indexed.edges_of(EdgeType.CALLS) == {
        ("sample_pkg.auth.authenticate", "sample_pkg.auth.validate"),
        ("sample_pkg.auth.make_user", "sample_pkg.models.User"),
    }


def test_imports_edges(indexed: _Indexed) -> None:
    assert indexed.edges_of(EdgeType.IMPORTS) == {
        ("sample_pkg.auth", "sample_pkg.models.User"),  # cross-file, in-repo
        ("sample_pkg.auth", "os"),  # external package
    }


def test_references_edges(indexed: _Indexed) -> None:
    # DEFAULT = User is a name load (not a call) resolving to an in-repo symbol.
    assert indexed.edges_of(EdgeType.REFERENCES) == {
        ("sample_pkg.auth", "sample_pkg.models.User"),
    }


def test_all_edges_are_extracted(indexed: _Indexed) -> None:
    assert all(e.confidence is Confidence.EXTRACTED for e in indexed.result.edges)
    assert all(n.confidence is Confidence.EXTRACTED for n in indexed.result.nodes)


def test_no_cfg_or_pdg_edges_in_v0(indexed: _Indexed) -> None:
    reserved = {EdgeType.CONTROL_FLOW, EdgeType.CONTROL_DEP, EdgeType.DATA_DEP}
    assert not any(e.type in reserved for e in indexed.result.edges)


def test_store_supports_duplicate_check(indexed: _Indexed) -> None:
    assert indexed.store.exists("authenticate(name)") is True
    assert indexed.store.get_symbol("sample_pkg.auth.validate") is not None


def test_resolution_emits_span(indexed: _Indexed) -> None:
    assert any(s.phase is Phase.RESOLUTION for s in indexed.recorder.spans)


def test_decorated_function_calls_still_resolve(tmp_path: Path) -> None:
    # tree-sitter and jedi both anchor a decorated def to its ``def`` line, not the
    # decorator line — so call resolution to a decorated target must still match.
    pkg = tmp_path / "deco_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(
        "import functools\n\n\n"
        "@functools.cache\n"
        "def base():\n"
        "    return 1\n\n\n"
        "def caller():\n"
        "    return base()\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    result = index_package(pkg, repo_id="deco", store=store)
    by_id = {n.id: n for n in result.nodes}
    calls = {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in result.edges
        if e.type is EdgeType.CALLS
    }
    assert ("deco_pkg.m.caller", "deco_pkg.m.base") in calls


def test_methods_with_same_signature_are_not_duplicates(tmp_path: Path) -> None:
    # End-to-end: the extractor must mark methods is_top_level=False so the duplicate gate
    # does not flag e.g. an ABC method and its impl. Two FREE functions with the same
    # signature in different modules ARE genuine duplicates and must still be flagged.
    from cartogate.surfaces import find_duplicate_signatures

    pkg = tmp_path / "mp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("class A:\n    def run(self, x):\n        return x\n", "utf-8")
    (pkg / "b.py").write_text("class B:\n    def run(self, x):\n        return x\n", "utf-8")
    (pkg / "u1.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    (pkg / "u2.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")

    store = InMemoryStore()
    result = index_package(pkg, repo_id="t", store=store)
    dups = find_duplicate_signatures(list(result.nodes))

    # A.run / B.run (methods) are NOT duplicates; the free function helper IS.
    assert set(dups) == {(Language.PYTHON, "helper(x)")}
    members = dups[(Language.PYTHON, "helper(x)")]
    assert {n.qualified_name for n in members} == {"mp.u1.helper", "mp.u2.helper"}


def test_index_skips_noise_directories(tmp_path: Path) -> None:
    # Indexing must skip .venv/site-packages etc. — otherwise third-party symbols pollute
    # the table and cause false-positive duplicate verdicts (review finding; F-38).
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / ".venv" / "lib").mkdir(parents=True)
    (proj / "src" / "__init__.py").write_text("", "utf-8")
    (proj / "src" / "app.py").write_text("def real_fn():\n    return 1\n", "utf-8")
    (proj / ".venv" / "lib" / "vendor.py").write_text("def vendored_fn():\n    return 1\n", "utf-8")

    store = InMemoryStore()
    result = index_package(proj, repo_id="t", store=store)
    symbols = {n.qualified_name for n in result.nodes if n.kind is NodeKind.SYMBOL}
    assert any(s.endswith(".real_fn") for s in symbols)
    assert not any("vendor" in s for s in symbols)  # .venv was excluded


def test_resolve_false_builds_signature_table_without_resolution() -> None:
    # The resolution-free fast path (for the latency-sensitive PreToolUse hook) must still
    # produce the full symbol table + signature index (so check_duplicate works), but no
    # resolved edges or external nodes.
    fast = index_package(FIXTURE_ROOT, repo_id="t", store=InMemoryStore(), resolve=False)
    full = index_package(FIXTURE_ROOT, repo_id="t", store=InMemoryStore(), resolve=True)

    fast_syms = {n.qualified_name for n in fast.nodes if n.kind is NodeKind.SYMBOL}
    full_syms = {n.qualified_name for n in full.nodes if n.kind is NodeKind.SYMBOL}
    assert fast_syms == full_syms  # same symbols extracted

    fast_edge_types = {e.type for e in fast.edges}
    assert EdgeType.DEFINES in fast_edge_types  # structural edges still built
    assert EdgeType.CALLS not in fast_edge_types  # resolved edges skipped
    assert EdgeType.IMPORTS not in fast_edge_types
    assert not any(n.kind is NodeKind.EXTERNAL_PACKAGE for n in fast.nodes)

    # The duplicate gate works on the fast index.
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store, resolve=False)
    assert store.exists("authenticate(name)") is True


def test_external_packages_named_by_package_not_symbol(tmp_path: Path) -> None:
    # `from collections.abc import Iterable` must yield an external node named for the
    # PACKAGE (collections), not the imported symbol (Iterable). And importing an in-repo
    # module-level constant must link to its owning module, not create a bogus external.
    pkg = tmp_path / "mp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", "utf-8")
    (pkg / "consts.py").write_text("THE_CONST = 42\n", "utf-8")
    (pkg / "uses.py").write_text(
        "import os\n"
        "from collections.abc import Iterable\n"
        "from .consts import THE_CONST\n"
        "\n"
        "def f(x):\n"
        "    return x\n",
        "utf-8",
    )
    store = InMemoryStore()
    result = index_package(pkg, repo_id="t", store=store)

    external = {n.qualified_name for n in result.nodes if n.kind is NodeKind.EXTERNAL_PACKAGE}
    assert external == {"os", "collections"}
    assert "Iterable" not in external
    assert "THE_CONST" not in external

    by_id = {n.id: n for n in result.nodes}
    imports = {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in result.edges
        if e.type is EdgeType.IMPORTS
    }
    # The in-repo constant import resolves to its owning module.
    assert ("mp.uses", "mp.consts") in imports


def test_wildcard_import_records_module_dependency(tmp_path: Path) -> None:
    # `from .base import *` — the imported names are unknown, but the dependency on the
    # module must still be recorded so it stays visible in the graph.
    pkg = tmp_path / "wp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", "utf-8")
    (pkg / "base.py").write_text("def helper():\n    return 1\n", "utf-8")
    (pkg / "uses.py").write_text("from .base import *\n\n\ndef f():\n    return 1\n", "utf-8")
    result = index_package(pkg, repo_id="t", store=InMemoryStore())
    by_id = {n.id: n for n in result.nodes}
    imports = {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in result.edges
        if e.type is EdgeType.IMPORTS
    }
    assert ("wp.uses", "wp.base") in imports


def test_scip_artifact_parses(indexed: _Indexed, tmp_path: Path) -> None:
    out = tmp_path / "index.scip.json"
    emit_scip(indexed.result, out, repo_id=REPO)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["tool"]["name"] == "cartogate"
    doc_paths = {d["relative_path"] for d in payload["documents"]}
    assert any(p.endswith("auth.py") for p in doc_paths)
    # Every document declares at least one symbol occurrence.
    assert all("occurrences" in d for d in payload["documents"])
