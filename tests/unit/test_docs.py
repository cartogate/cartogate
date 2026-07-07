"""Unit tests for the markdown doc extractor (explicit doc→code references)."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import MakeSymbol

from cartogate.extract.docs import SymbolIndex, extract_doc_facts
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance


def _module(make_symbol: MakeSymbol, qname: str, unit: str):
    # Reuse the symbol factory but stamp a MODULE node by overriding via a fresh build.
    node = make_symbol(qname, unit=unit)
    return node.model_copy(update={"kind": NodeKind.MODULE})


def test_symbol_index_matches_conservatively(make_symbol: MakeSymbol) -> None:
    foo = make_symbol("pkg.a.foo", unit="pkg/a.py")
    bar = make_symbol("pkg.a.bar", unit="pkg/a.py")
    dup1 = make_symbol("pkg.a.run", unit="pkg/a.py")
    dup2 = make_symbol("pkg.b.run", unit="pkg/b.py")  # same bare name 'run' -> ambiguous
    index = SymbolIndex([foo, bar, dup1, dup2], modules=[])

    assert index.match_span("pkg.a.foo") is foo  # exact qualified name
    assert index.match_span("foo") is foo  # unique bare name
    assert index.match_span("foo()") is foo  # trailing () stripped
    assert index.match_span("run") is None  # ambiguous bare name -> skipped
    assert index.match_span("pkg.a.run") is dup1  # but the qualified name disambiguates
    assert index.match_span("nonexistent") is None


def test_symbol_index_matches_file_links(make_symbol: MakeSymbol) -> None:
    mod = _module(make_symbol, "proj.pkg.auth", unit="proj/pkg/auth.py")
    index = SymbolIndex([], modules=[mod])
    assert index.match_link("proj/pkg/auth.py") is mod
    assert index.match_link("pkg/auth.py") is mod  # suffix match
    assert index.match_link("./proj/pkg/auth.py") is mod
    assert index.match_link("other.py") is None


def test_extract_doc_facts(tmp_path: Path, make_symbol: MakeSymbol) -> None:
    (tmp_path / "README.md").write_text(
        "# Auth\n\nThe `authenticate` function logs a user in; see [code](pkg/auth.py).\n"
        "Unrelated `mystery` is not a symbol.\n",
        encoding="utf-8",
    )
    (tmp_path / "EMPTY.md").write_text("# Nothing\n\nNo code references here.\n", encoding="utf-8")

    auth = make_symbol("proj.pkg.auth.authenticate", unit="proj/pkg/auth.py")
    module = _module(make_symbol, "proj.pkg.auth", unit="proj/pkg/auth.py")
    facts = extract_doc_facts(
        tmp_path, repo_id="proj", base=tmp_path.parent, symbols=[auth], modules=[module]
    )

    # README references authenticate (code span) + the module (link); EMPTY.md is dropped.
    doc_nodes = [n for n in facts.nodes if n.kind is NodeKind.DOC_SECTION]
    assert {n.name for n in doc_nodes} == {"README.md"}
    readme = doc_nodes[0]
    assert readme.provenance is Provenance.DOC
    assert readme.confidence is Confidence.EXTRACTED

    targets = {(e.src, e.dst) for e in facts.edges if e.type is EdgeType.DOCUMENTS}
    assert (readme.id, auth.id) in targets
    assert (readme.id, module.id) in targets
    assert all(e.confidence is Confidence.EXTRACTED for e in facts.edges)
