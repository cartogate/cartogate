"""doc-drift over a really-indexed package (markdown referencing code)."""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.flag import FlagEngine
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools, dispatch
from cartogate.schema.enums import NodeKind
from cartogate.store import InMemoryStore


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "auth.py").write_text(
        "def authenticate(name):\n    return bool(name)\n", "utf-8"
    )
    (proj / "README.md").write_text(
        "# Proj\n\nCall `authenticate` to log in. See [src](pkg/auth.py).\n", "utf-8"
    )
    return proj


def _indexed(tmp_path: Path) -> InMemoryStore:
    store = InMemoryStore()
    index_package(_make_proj(tmp_path), repo_id="proj", store=store)
    return store


def test_docs_for_symbols_flags_the_readme(tmp_path: Path) -> None:
    engine = FlagEngine(_indexed(tmp_path))
    report = engine.docs_for_symbols(["proj.pkg.auth.authenticate"]).to_dict()
    assert any(d["path"].endswith("README.md") for d in report["docs"])


def test_docs_for_diff_flags_the_readme(tmp_path: Path) -> None:
    diff = (
        "--- a/proj/pkg/auth.py\n"
        "+++ b/proj/pkg/auth.py\n"
        "@@ -1 +1 @@\n"
        "-def authenticate(name):\n"
        "+def authenticate(user):\n"
    )
    report = FlagEngine(_indexed(tmp_path)).docs_for_diff(diff).to_dict()
    assert any(d["path"].endswith("README.md") for d in report["docs"])


def test_doc_drift_mcp_tool(tmp_path: Path) -> None:
    tools = CartogateTools(_indexed(tmp_path))
    out = dispatch(tools, "doc_drift", {"symbols": ["proj.pkg.auth.authenticate"]})
    assert out["count"] >= 1


def test_doc_nodes_do_not_affect_the_duplicate_gate(tmp_path: Path) -> None:
    # docs are advisory — a doc_section must not pollute the signature index.
    store = InMemoryStore()
    result = index_package(_make_proj(tmp_path), repo_id="proj", store=store)
    # Docs ARE extracted (so this isolation is meaningful)...
    doc_nodes = [n for n in result.nodes if n.kind is NodeKind.DOC_SECTION]
    assert doc_nodes, "expected a doc_section node from README.md"
    # ...but carry no signature, so they cannot enter the duplicate sig-index.
    assert all(n.signature is None for n in doc_nodes)
    assert store.exists("authenticate(name)") is True  # the real function is still gate-able


def test_doc_pass_respects_gitignore(tmp_path: Path) -> None:
    """Docs extraction takes the same git working set as sources - gitignored markdown
    (vendored trees, build output) never becomes doc nodes."""
    import subprocess

    proj = _make_proj(tmp_path)
    (proj / "generated").mkdir()
    (proj / "generated" / "api.md").write_text("# generated md `authenticate`", "utf-8")
    (proj / ".gitignore").write_text("generated/", "utf-8")
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, capture_output=True)

    store = InMemoryStore()
    index_package(proj, repo_id="proj", store=store)
    doc_units = {
        n.unit
        for i in store.visible_node_ids()
        if (n := store.get_node(i)) is not None and n.kind is NodeKind.DOC_SECTION
    }
    assert any("README.md" in u for u in doc_units)  # the tracked doc is indexed
    assert not any("generated" in u for u in doc_units)  # the gitignored one is not


def test_doc_pass_includes_untracked_but_not_ignored_docs(tmp_path: Path) -> None:
    """A draft README that exists but was never `git add`ed still indexes — the working set
    is --cached --others --exclude-standard, so only GITIGNORED files are out."""
    import subprocess

    proj = _make_proj(tmp_path)
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True, capture_output=True)
    (proj / "DRAFT.md").write_text("# draft notes on `authenticate`", "utf-8")  # NOT added

    store = InMemoryStore()
    index_package(proj, repo_id="proj", store=store)
    doc_units = {
        n.unit
        for i in store.visible_node_ids()
        if (n := store.get_node(i)) is not None and n.kind is NodeKind.DOC_SECTION
    }
    assert any("DRAFT.md" in u for u in doc_units)
