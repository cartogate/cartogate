"""F-36 Stage A: re-extracting one file against a store-derived ResolutionContext keeps its
cross-file *resolved* edges (the thing a naive ``paths=`` subset re-index drops).

The pipeline piece only: ``build_resolution_context(store, base)`` rebuilds the whole-repo maps from
the store's visible nodes, and ``index_package(..., context=...)`` seeds resolution from them so a
changed file resolves against the entire repo, not just itself.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.pipeline import build_resolution_context, index_package
from cartogate.schema.enums import EdgeType, NodeKind
from cartogate.store import InMemoryStore

_A = "def helper():\n    return 1\n"
_B = "from pkg.a import helper\n\n\ndef run():\n    return helper()\n"
_B_REL = "pkg/b.py"


def _pkg(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(_A, encoding="utf-8")
    (pkg / "b.py").write_text(_B, encoding="utf-8")


def _full_index(tmp_path: Path) -> InMemoryStore:
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def _edges_of(store: InMemoryStore, edge_type: EdgeType) -> set[tuple[str, str]]:
    """Visible edges of ``edge_type`` as (src_qname, dst_qname) pairs."""
    sub = store.subgraph(store.visible_node_ids(), edge_types=(edge_type,))
    by_id = {n.id: n for n in sub.nodes}
    return {
        (by_id[e.src].qualified_name, by_id[e.dst].qualified_name)
        for e in sub.edges
        if e.src in by_id and e.dst in by_id
    }


def _calls(store: InMemoryStore) -> set[tuple[str, str]]:
    return _edges_of(store, EdgeType.CALLS)


def _externals(store: InMemoryStore) -> set[str]:
    return {
        n.qualified_name
        for n in store.subgraph(store.visible_node_ids()).nodes
        if n.kind is NodeKind.EXTERNAL_PACKAGE
    }


def _carry_forward_except(src: InMemoryStore, rel: str) -> InMemoryStore:
    """A new store with every unit of ``src`` except ``rel`` (mirrors the daemon carry-forward)."""
    new = InMemoryStore()
    new.bulk_load((u, n, e) for u, n, e in src.iter_unit_facts() if u != rel)
    return new


def test_context_seeded_reindex_resolves_cross_file_call(tmp_path: Path) -> None:
    _pkg(tmp_path)
    store1 = _full_index(tmp_path)
    assert ("pkg.b.run", "pkg.a.helper") in _calls(store1)  # baseline: full index links b -> a

    # Re-extract ONLY b.py, resolving against a context built from the rest of the repo.
    ctx = build_resolution_context(store1, tmp_path, exclude_rels={_B_REL})
    store2 = _carry_forward_except(store1, _B_REL)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path,
        paths=[tmp_path / "pkg" / "b.py"], context=ctx,
    )
    assert ("pkg.b.run", "pkg.a.helper") in _calls(store2)  # cross-file edge survives re-extract


def test_reindex_without_context_drops_cross_file_call(tmp_path: Path) -> None:
    # Pins the defect F-36 fixes: a naive paths= subset re-index resolves only against itself, so a
    # call into the unchanged file is dropped — even though a.helper is present in the store.
    _pkg(tmp_path)
    store1 = _full_index(tmp_path)
    store2 = _carry_forward_except(store1, _B_REL)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path, paths=[tmp_path / "pkg" / "b.py"]
    )
    assert store2.get_symbol("pkg.a.helper") is not None  # the target IS in the store
    assert ("pkg.b.run", "pkg.a.helper") not in _calls(store2)  # ...but the edge was dropped


def test_build_resolution_context_scopes_to_unchanged_files(tmp_path: Path) -> None:
    _pkg(tmp_path)
    store1 = _full_index(tmp_path)
    ctx = build_resolution_context(store1, tmp_path, exclude_rels={_B_REL})
    qnames = {node.qualified_name for node in ctx.symboldef_by_loc.values()}
    assert "pkg.a.helper" in qnames  # unchanged file's symbols are in the resolution table
    assert "pkg.b.run" not in qnames  # the excluded (about-to-be-reextracted) file's are not


def test_context_seeded_reindex_resolves_cross_file_module_import(tmp_path: Path) -> None:
    # Exercises the seeded `module_by_abspath` path (def_type == "module"), distinct from the
    # symbol path the other tests hit: `import pkg.a` resolves to a's MODULE node.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(_A, encoding="utf-8")
    (pkg / "b.py").write_text("import pkg.a\n\n\ndef run():\n    return pkg.a.helper()\n", "utf-8")
    store1 = _full_index(tmp_path)
    # `import pkg.a` resolves to the in-repo `pkg` package module via module_by_abspath (__init__).
    assert ("pkg.b", "pkg") in _edges_of(store1, EdgeType.IMPORTS)
    assert store1.get_symbol("pkg") is not None  # target is a real in-repo module, not external

    ctx = build_resolution_context(store1, tmp_path, exclude_rels={_B_REL})
    store2 = _carry_forward_except(store1, _B_REL)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path,
        paths=[tmp_path / "pkg" / "b.py"], context=ctx,
    )
    assert ("pkg.b", "pkg") in _edges_of(store2, EdgeType.IMPORTS)  # module-import edge survives


def test_context_seeded_reindex_resolves_cross_file_inherits(tmp_path: Path) -> None:
    # INHERITS goes through the same symboldef_by_loc path as CALLS — confirm another edge type.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (pkg / "b.py").write_text("from pkg.a import Base\n\n\nclass Child(Base):\n    pass\n", "utf-8")
    store1 = _full_index(tmp_path)
    assert ("pkg.b.Child", "pkg.a.Base") in _edges_of(store1, EdgeType.INHERITS)

    ctx = build_resolution_context(store1, tmp_path, exclude_rels={_B_REL})
    store2 = _carry_forward_except(store1, _B_REL)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path,
        paths=[tmp_path / "pkg" / "b.py"], context=ctx,
    )
    assert ("pkg.b.Child", "pkg.a.Base") in _edges_of(store2, EdgeType.INHERITS)


def test_reextract_target_with_moved_symbol_keeps_incoming_edge(tmp_path: Path) -> None:
    # The complement: re-extract the file a symbol lives in, with that symbol MOVED to a new line.
    # The fresh node wins (context excludes a.py) and the importer's incoming edge survives because
    # ids are content/line-independent.
    _pkg(tmp_path)
    store1 = _full_index(tmp_path)
    assert ("pkg.b.run", "pkg.a.helper") in _calls(store1)

    a_rel = "pkg/a.py"
    (tmp_path / "pkg" / "a.py").write_text("\n\n" + _A, encoding="utf-8")  # helper now at line 3
    ctx = build_resolution_context(store1, tmp_path, exclude_rels={a_rel})
    store2 = _carry_forward_except(store1, a_rel)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path,
        paths=[tmp_path / "pkg" / "a.py"], context=ctx,
    )
    helper = store2.get_symbol("pkg.a.helper")
    assert helper is not None and helper.location.start_line == 3  # fresh node at the new line
    assert ("pkg.b.run", "pkg.a.helper") in _calls(store2)  # b's incoming edge survived the move


def test_context_unions_externals_not_drops_them(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from os import getpid\n\n\ndef f():\n    return getpid()\n", "utf-8")
    (pkg / "b.py").write_text("from sys import argv\n\n\ndef g():\n    return argv\n", "utf-8")
    store1 = _full_index(tmp_path)
    assert {"os", "sys"} <= _externals(store1)

    ctx = build_resolution_context(store1, tmp_path, exclude_rels={_B_REL})
    store2 = _carry_forward_except(store1, _B_REL)
    index_package(
        tmp_path, repo_id="pkg", store=store2, base=tmp_path,
        paths=[tmp_path / "pkg" / "b.py"], context=ctx,
    )
    # Re-extracting b (which imports sys) must NOT drop a's external (os) from the <externals> unit.
    assert {"os", "sys"} <= _externals(store2)
