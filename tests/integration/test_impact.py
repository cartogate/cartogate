"""PR-time impact summary (F-68) — composes affected-code + tests + docs over changed symbols."""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.impact import build_impact_summary, changed_symbol_qnames
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def _index(tmp_path: Path, files: dict[str, str]) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        path = pkg / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def test_summary_composes_affected_tests_and_docs(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "core.py": "def target():\n    return 1\n\ndef caller():\n    return target()\n",
            "test_core.py": (
                "from pkg.core import target\n\ndef test_target():\n    assert target()\n"
            ),
            "README.md": "See [`pkg.core.target`](pkg/core.py) for details.\n",
        },
    )
    summary = build_impact_summary(store, ["pkg.core.target"])
    data = summary.to_dict()

    assert data["changed"] == ["pkg.core.target"]
    affected = {a["qualified_name"] for a in data["affected"]}
    assert "pkg.core.caller" in affected  # caller depends on target -> blast radius
    assert "pkg.core.target" not in affected  # the changed symbol itself is not "affected"
    tests = {t["qualified_name"] for t in data["tests"]}
    assert any("test_target" in t for t in tests)  # FLAG: the exercising test
    assert data["counts"]["changed"] == 1


def test_markdown_renders_and_is_deterministic(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {"core.py": "def target():\n    return 1\n\ndef caller():\n    return target()\n"},
    )
    summary = build_impact_summary(store, ["pkg.core.target"])
    md = summary.to_markdown()
    assert "Cartogate impact summary" in md
    assert "pkg.core.caller" in md  # affected listed
    assert summary.to_markdown() == md  # deterministic
    assert build_impact_summary(store, ["pkg.core.target"]).to_dict() == summary.to_dict()


def test_empty_and_unknown_symbols(tmp_path: Path) -> None:
    store = _index(tmp_path, {"core.py": "def target():\n    return 1\n"})
    # An unknown symbol contributes nothing; target has no callers/tests/docs -> all empty.
    summary = build_impact_summary(store, ["pkg.core.does_not_exist"])
    assert summary.changed == ()
    assert summary.to_dict()["counts"] == {"changed": 0, "affected": 0, "tests": 0, "docs": 0}
    assert "No indexed symbols changed" in summary.to_markdown()


def test_changed_symbol_qnames_from_diff_regions(tmp_path: Path) -> None:
    from cartogate.engine.diff import parse_unified_diff

    store = _index(
        tmp_path,
        {"core.py": "def target():\n    return 1\n\ndef other():\n    return 2\n"},
    )
    # A diff touching line 2 (inside target) -> target is the changed symbol.
    diff = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
        "@@ -2 +2 @@\n-    return 1\n+    return 11\n"
    )
    qnames = changed_symbol_qnames(store, parse_unified_diff(diff))
    assert "pkg.core.target" in qnames
    assert "pkg.core.other" not in qnames
