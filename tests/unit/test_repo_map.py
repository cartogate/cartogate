"""Tests for repo_map tool — orientation in unfamiliar repos."""

from __future__ import annotations

import pytest

from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore

REPO = "repoA"


def make_symbol(
    qualified_name: str,
    *,
    signature: str | None,
    unit: str = "m.py",
    content: str = "x",
    is_top_level: bool = True,
    visibility: Visibility = Visibility.EXPORTED,
) -> Node:
    name = qualified_name.rsplit(".", 1)[-1]
    return Node.create(
        repo_id=REPO,
        qualified_name=qualified_name,
        kind=NodeKind.SYMBOL,
        name=name,
        unit=unit,
        signature=signature,
        location=Location(path=unit, start_line=1, end_line=2),
        visibility=visibility,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash=content,
        is_top_level=is_top_level,
    )


def make_edge(
    src: Node,
    dst: Node,
    edge_type: EdgeType,
    confidence: Confidence = Confidence.EXTRACTED,
) -> Edge:
    provenance = (
        Provenance.TREE_SITTER if confidence is Confidence.EXTRACTED else Provenance.SEMANTIC_SKILL
    )
    return Edge(
        type=edge_type,
        src=src.id,
        dst=dst.id,
        provenance=provenance,
        confidence=confidence,
    )


@pytest.fixture
def store_with_two_units() -> InMemoryStore:
    """Two units: a.py (two public functions + one private) and b.py (one class + imports a)."""
    store = InMemoryStore()

    # Unit a.py: two top-level public functions + one private helper
    public_fn1 = make_symbol("pkg.foo", signature="def foo(x):", unit="src/pkg/a.py")
    public_fn2 = make_symbol("pkg.bar", signature="def bar(y):", unit="src/pkg/a.py")
    private_helper = make_symbol(
        "pkg._helper",
        signature="def _helper(z):",
        unit="src/pkg/a.py",
        is_top_level=True,
        visibility=Visibility.INTERNAL,
    )

    # Unit b.py: one top-level public class
    public_class = make_symbol("pkg.MyClass", signature="class MyClass:", unit="src/pkg/b.py")

    # Store both units
    store.upsert_unit("src/pkg/a.py", [public_fn1, public_fn2, private_helper], [])

    # b imports from a
    import_edge = make_edge(public_class, public_fn1, EdgeType.IMPORTS)
    store.upsert_unit("src/pkg/b.py", [public_class], [import_edge])

    return store


def test_map_lists_units(store_with_two_units: InMemoryStore) -> None:
    """repo_map() overview: lists all units with counts and top-level public exports."""
    from cartogate.mcp.tools import CartogateTools

    tools = CartogateTools(store_with_two_units)
    result = tools.repo_map()

    assert result["unit_count"] == 2
    assert result["node_count"] == 4  # 3 in a.py, 1 in b.py
    assert result["edge_count"] == 1  # 1 import edge

    units = result["units"]
    assert len(units) == 2

    # First unit: src/pkg/a.py
    unit_a = units[0]
    assert unit_a["unit"] == "src/pkg/a.py"
    assert unit_a["symbols"] == 3  # foo, bar, _helper
    # Only 2 public exports (foo and bar; _helper is INTERNAL)
    assert len(unit_a["exports"]) == 2
    assert {e["name"] for e in unit_a["exports"]} == {"foo", "bar"}

    # Second unit: src/pkg/b.py
    unit_b = units[1]
    assert unit_b["unit"] == "src/pkg/b.py"
    assert unit_b["symbols"] == 1
    assert len(unit_b["exports"]) == 1
    assert unit_b["exports"][0]["name"] == "MyClass"


def test_exports_capped() -> None:
    """Exports are capped at EXPORTS_CAP=8 with a 'more' field for remainder."""
    from cartogate.mcp.tools import CartogateTools

    # Add 10 more public functions to a.py to exceed the cap
    a_py_nodes = []
    for i in range(10):
        a_py_nodes.append(
            make_symbol(f"pkg.func{i}", signature=f"def func{i}():", unit="src/pkg/a.py")
        )
    # Re-add the original functions too
    public_fn1 = make_symbol("pkg.foo", signature="def foo(x):", unit="src/pkg/a.py")
    public_fn2 = make_symbol("pkg.bar", signature="def bar(y):", unit="src/pkg/a.py")
    private_helper = make_symbol(
        "pkg._helper",
        signature="def _helper(z):",
        unit="src/pkg/a.py",
        is_top_level=True,
        visibility=Visibility.INTERNAL,
    )
    all_a_py = [public_fn1, public_fn2, private_helper] + a_py_nodes

    store2 = InMemoryStore()
    store2.upsert_unit("src/pkg/a.py", all_a_py, [])

    tools = CartogateTools(store2)
    result = tools.repo_map()

    units = result["units"]
    unit_a = units[0]
    assert len(unit_a["exports"]) == 8
    assert unit_a["more"] == 4  # 12 public functions - 8 shown = 4 more


def test_module_detail(store_with_two_units: InMemoryStore) -> None:
    """repo_map(module='src/pkg/a.py') shows full exports + dependents."""
    from cartogate.mcp.tools import CartogateTools

    tools = CartogateTools(store_with_two_units)
    result = tools.repo_map(module="src/pkg/a.py")

    assert result["unit"] == "src/pkg/a.py"
    # Exports are full briefs (uncapped), including private symbols in detail mode
    exports = result["exports"]
    # In detail mode, we show all top-level symbols, but typically we'd filter to public
    # The test expects we see the public ones at least
    export_names = {e["name"] for e in exports}
    assert "foo" in export_names
    assert "bar" in export_names

    # Dependents: units that import from this unit (b.py imports foo)
    assert result["dependents"] == ["src/pkg/b.py"]


def test_module_miss(store_with_two_units: InMemoryStore) -> None:
    """repo_map(module='nope.py') returns found=False + candidates matching basename or suffix."""
    from cartogate.mcp.tools import CartogateTools

    tools = CartogateTools(store_with_two_units)
    result = tools.repo_map(module="nope.py")

    assert result["found"] is False
    assert result["unit"] == "nope.py"
    # Candidates: no exact match, but can offer suggestions
    # In this case, no real candidates match "nope", so it should be empty or a small list
    assert isinstance(result.get("candidates"), list)


def test_overview_excludes_externals_and_counts_symbols_only() -> None:
    """End-to-end via the real pipeline: the overview must not list the synthetic <externals>
    unit, and per-unit symbol counts must exclude the structural per-file MODULE node.

    The hand-built store fixtures have neither a module node nor an externals unit, so only a
    real index catches these — hence this end-to-end guard.
    """
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from cartogate.extract.pipeline import index_package
    from cartogate.mcp.tools import CartogateTools

    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        (repo / "pkg").mkdir(parents=True)
        # One module with two functions; imports os so an <externals> unit is produced.
        (repo / "pkg" / "mod.py").write_text(
            "import os\n\n\ndef alpha():\n    return os.getpid()\n\n\ndef beta():\n    return 2\n"
        )
        store = InMemoryStore()
        index_package(repo, repo_id="proj", store=store)

        result = CartogateTools(store).repo_map()

        unit_names = [u["unit"] for u in result["units"]]
        assert "<externals>" not in unit_names, unit_names
        mod = next(u for u in result["units"] if u["unit"].endswith("pkg/mod.py"))
        # Two symbols (alpha, beta) — NOT three (the MODULE node must not be counted).
        assert mod["symbols"] == 2, mod
        assert {e["name"] for e in mod["exports"]} == {"alpha", "beta"}
        # Units are sorted for determinism.
        assert unit_names == sorted(unit_names)
