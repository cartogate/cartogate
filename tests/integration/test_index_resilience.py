"""Index resilience: a name-resolver failure on one file degrades it to structural-only instead of
aborting the whole index (the jedi-crash footgun). Resolution is advisory — the gate is unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.schema.enums import EdgeType
from cartogate.store import InMemoryStore

_SRC = "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n"


def _pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(_SRC, encoding="utf-8")
    return tmp_path


def _index(tmp_path: Path):
    store = InMemoryStore()
    return store, index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)


def test_baseline_resolves_call_edge(tmp_path: Path) -> None:
    # Sanity: without any injected failure, run() -> helper() resolves and nothing is degraded.
    _pkg(tmp_path)
    _store, result = _index(tmp_path)
    assert result.resolution_failures == 0
    assert any(e.type == EdgeType.CALLS for e in result.edges)  # run -> helper resolved


def test_resolve_crash_degrades_file_not_whole_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the jedi crash: every resolve() raises. The index must still COMPLETE with the
    # structural facts (symbol + defines), record the failure, and drop only the resolved edges.
    _pkg(tmp_path)

    def _boom(self: object, abs_path: str, line: int, column: int) -> None:
        raise RuntimeError("simulated jedi cache corruption")

    monkeypatch.setattr("cartogate.extract.resolver.JediResolver.resolve", _boom)

    store, result = _index(tmp_path)  # must NOT raise

    qnames = {n.qualified_name for n in result.nodes}
    assert "pkg.core.helper" in qnames and "pkg.core.run" in qnames  # structural survived
    assert result.resolution_failures == 1  # exactly the one file, surfaced not silent
    assert any(e.type == EdgeType.DEFINES for e in result.edges)  # structural edges kept
    assert not any(e.type == EdgeType.CALLS for e in result.edges)  # resolved edges degraded away
    # The duplicate gate rests on the signature table, which is intact -> still works.
    assert store.exists("def helper():")


def test_resolver_build_crash_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the resolver can't even be built (constructor raises), the language degrades to
    # structural-only and the index still completes.
    _pkg(tmp_path)

    def _boom_init(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("resolver would not start")

    monkeypatch.setattr("cartogate.extract.resolver.JediResolver.__init__", _boom_init)

    store, result = _index(tmp_path)  # must NOT raise

    assert {"pkg.core.helper", "pkg.core.run"} <= {n.qualified_name for n in result.nodes}
    assert result.resolution_failures == 2  # both python files (__init__.py + core.py) in lang set
    assert not any(e.type == EdgeType.CALLS for e in result.edges)
    assert store.exists("def run():")  # gate signature table intact


def test_resolve_crash_mid_file_keeps_partial_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash AFTER some names resolved keeps the edges emitted before it (a valid partial result),
    # not just structural — and still counts the file as one failure.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # three calls -> three resolve() attempts; we crash on the 2nd so the 1st edge survives.
    (pkg / "m.py").write_text(
        "def a():\n    return 1\n\n\ndef b():\n    return a()\n\n\n"
        "def c():\n    return b() + a()\n",
        encoding="utf-8",
    )
    from cartogate.extract.resolver import JediResolver

    real = JediResolver.resolve
    calls = {"n": 0}

    def _boom_after_first(self: object, abs_path: str, line: int, column: int):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("crash after the first successful resolve")
        return real(self, abs_path, line, column)

    monkeypatch.setattr("cartogate.extract.resolver.JediResolver.resolve", _boom_after_first)

    _store, result = _index(tmp_path)  # must NOT raise
    assert result.resolution_failures == 1  # only m.py crashed (__init__ has no names)
    assert calls["n"] >= 2  # we got past the first resolve before crashing
    assert any(e.type == EdgeType.CALLS for e in result.edges)  # the pre-crash edge survived


def test_structural_walk_crash_skips_only_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A structural-walk crash on one file drops just that file (files_skipped), not the whole index.
    # A second, healthy file still indexes fully.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bad.py").write_text("def boom():\n    return 1\n", encoding="utf-8")
    (pkg / "good.py").write_text("def fine():\n    return 2\n", encoding="utf-8")

    from cartogate.extract.ast_walker import TreeSitterWalker

    real_walk = TreeSitterWalker.walk

    def _walk(self: object, source: bytes, **kwargs: object):
        if b"boom" in source:
            raise RuntimeError("tree-sitter blew up on this file")
        return real_walk(self, source, **kwargs)

    monkeypatch.setattr("cartogate.extract.ast_walker.TreeSitterWalker.walk", _walk)

    _store, result = _index(tmp_path)  # must NOT raise
    qnames = {n.qualified_name for n in result.nodes}
    assert result.files_skipped >= 1
    assert "pkg.good.fine" in qnames  # the healthy file indexed fully
    assert "pkg.bad.boom" not in qnames  # the crashing file was skipped entirely


def test_unreadable_file_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pkg(tmp_path)
    real_read = Path.read_text

    def _read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "core.py":
            raise OSError("permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read)
    _store, result = _index(tmp_path)  # must NOT raise
    assert result.files_skipped >= 1  # core.py dropped, index still completed
