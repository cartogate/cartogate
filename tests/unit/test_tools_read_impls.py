"""Tests for read_symbol (show actual code) and implementations (who implements/subclasses).

These tools address two constant agent questions: "show me this symbol's code" and "who
implements X" (grep is unreliable for cross-file inheritance; the graph has these edges already).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from cartogate.mcp.tools import CartogateTools
from cartogate.schema.edges import Edge
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node
from cartogate.store import InMemoryStore

REPO = "test_repo"


def make_symbol(
    qualified_name: str,
    *,
    signature: str | None,
    unit: str = "m.py",
    location: Location | None = None,
    is_top_level: bool = True,
) -> Node:
    name = qualified_name.rsplit(".", 1)[-1]
    if location is None:
        location = Location(path=unit, start_line=1, end_line=2)
    return Node.create(
        repo_id=REPO,
        qualified_name=qualified_name,
        kind=NodeKind.SYMBOL,
        name=name,
        unit=unit,
        signature=signature,
        location=location,
        visibility=Visibility.EXPORTED,
        provenance=Provenance.TREE_SITTER,
        confidence=Confidence.EXTRACTED,
        content_hash="test_content",
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


# ========================================================================== #
# implementations: who implements/subclasses X
# ========================================================================== #


def test_implementations_found_single() -> None:
    """Basic case: a base class with one subclass."""
    store = InMemoryStore()
    base = make_symbol("pkg.base.Base", signature="class Base:", unit="pkg/base.py")
    impl = make_symbol("pkg.impl.Impl", signature="class Impl:", unit="pkg/impl.py")
    store.upsert_unit("pkg/base.py", [base], [])
    store.upsert_unit("pkg/impl.py", [impl], [make_edge(impl, base, EdgeType.INHERITS)])

    result = CartogateTools(store).implementations("Base")

    assert result["found"] is True
    assert result["qualified_name"] == "pkg.base.Base"
    assert result["count"] == 1
    assert len(result["implementations"]) == 1
    impl_brief = result["implementations"][0]
    assert impl_brief["qualified_name"] == "pkg.impl.Impl"
    assert impl_brief["signature"] == "class Impl:"
    assert "location" in impl_brief


def test_implementations_found_multiple() -> None:
    """Multiple subclasses."""
    store = InMemoryStore()
    base = make_symbol("pkg.base.Base", signature="class Base:", unit="pkg/base.py")
    impl1 = make_symbol("pkg.impl1.Impl1", signature="class Impl1:", unit="pkg/impl1.py")
    impl2 = make_symbol("pkg.impl2.Impl2", signature="class Impl2:", unit="pkg/impl2.py")
    store.upsert_unit("pkg/base.py", [base], [])
    store.upsert_unit("pkg/impl1.py", [impl1], [make_edge(impl1, base, EdgeType.INHERITS)])
    store.upsert_unit("pkg/impl2.py", [impl2], [make_edge(impl2, base, EdgeType.INHERITS)])

    result = CartogateTools(store).implementations("Base")

    assert result["found"] is True
    assert result["count"] == 2
    names = sorted(b["qualified_name"] for b in result["implementations"])
    assert names == ["pkg.impl1.Impl1", "pkg.impl2.Impl2"]


def test_implementations_not_found() -> None:
    """Unknown symbol returns found=False with candidates."""
    store = InMemoryStore()
    dummy = make_symbol("pkg.dummy.Dummy", signature="class Dummy:", unit="pkg/dummy.py")
    store.upsert_unit("pkg/dummy.py", [dummy], [])

    result = CartogateTools(store).implementations("Unknown")

    assert result["found"] is False
    assert "candidates" in result


def test_implementations_bare_name() -> None:
    """Can resolve by bare name (Base) as well as fully qualified."""
    store = InMemoryStore()
    base = make_symbol("pkg.base.Base", signature="class Base:", unit="pkg/base.py")
    impl = make_symbol("pkg.impl.Impl", signature="class Impl:", unit="pkg/impl.py")
    store.upsert_unit("pkg/base.py", [base], [])
    store.upsert_unit("pkg/impl.py", [impl], [make_edge(impl, base, EdgeType.INHERITS)])

    result = CartogateTools(store).implementations("pkg.base.Base")

    assert result["found"] is True
    assert result["count"] == 1


def test_implementations_sorted_by_name() -> None:
    """Implementations sorted alphabetically by qualified name."""
    store = InMemoryStore()
    base = make_symbol("pkg.base.Base", signature="class Base:", unit="pkg/base.py")
    impl_z = make_symbol("pkg.z.Z", signature="class Z:", unit="pkg/z.py")
    impl_a = make_symbol("pkg.a.A", signature="class A:", unit="pkg/a.py")
    impl_m = make_symbol("pkg.m.M", signature="class M:", unit="pkg/m.py")
    store.upsert_unit("pkg/base.py", [base], [])
    store.upsert_unit("pkg/z.py", [impl_z], [make_edge(impl_z, base, EdgeType.INHERITS)])
    store.upsert_unit("pkg/a.py", [impl_a], [make_edge(impl_a, base, EdgeType.INHERITS)])
    store.upsert_unit("pkg/m.py", [impl_m], [make_edge(impl_m, base, EdgeType.INHERITS)])

    result = CartogateTools(store).implementations("Base")

    names = [b["qualified_name"] for b in result["implementations"]]
    assert names == ["pkg.a.A", "pkg.m.M", "pkg.z.Z"]


# ========================================================================== #
# read_symbol: show actual source code
# ========================================================================== #


def test_read_symbol_found_with_root() -> None:
    """Read the actual source code of a symbol when root is provided.

    Mirrors production: ``location.path`` is repo-prefixed (relative to ``root.parent``,
    the index base) and ``root`` is the repo dir itself — so read_symbol must resolve
    against ``root.parent``, not ``root`` (regression for the doubled-segment bug).
    """
    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        repo.mkdir()
        # Create a file with known content on lines 3-6
        file_path = repo / "module.py"
        file_content = "# header\n# line 2\ndef foo(x):\n    return x + 1\n# footer\n"
        file_path.write_text(file_content)

        # Node spans lines 3-5 (def, body, footer); path carries the repo prefix, exactly as
        # the extract pipeline (base=root.parent) records it.
        node = make_symbol(
            "pkg.foo",
            signature="def foo(x):",
            unit="proj/module.py",
            location=Location(path="proj/module.py", start_line=3, end_line=5),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/module.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("foo")

        assert result["found"] is True
        assert result["qualified_name"] == "pkg.foo"
        assert result["signature"] == "def foo(x):"
        assert result["location"] == "proj/module.py:3-5"
        assert result["truncated"] is False
        source_lines = result["source"].split("\n")
        assert len(source_lines) == 3
        assert source_lines[0] == "def foo(x):"
        assert source_lines[1] == "    return x + 1"
        assert source_lines[2] == "# footer"


def test_read_symbol_not_found() -> None:
    """Unknown symbol returns found=False."""
    with TemporaryDirectory() as tmpdir:
        store = InMemoryStore()
        result = CartogateTools(store, root=tmpdir).read_symbol("Unknown")
        assert result["found"] is False
        assert "candidates" in result


def test_read_symbol_no_root_provided() -> None:
    """When root is not provided, source should be None with a note."""
    node = make_symbol(
        "pkg.foo",
        signature="def foo(x):",
        unit="module.py",
        location=Location(path="module.py", start_line=3, end_line=6),
    )
    store = InMemoryStore()
    store.upsert_unit("module.py", [node], [])

    result = CartogateTools(store).read_symbol("foo")

    assert result["found"] is True
    assert result["qualified_name"] == "pkg.foo"
    assert result["source"] is None
    assert "note" in result
    assert "workspace root unknown" in result["note"].lower()


def test_read_symbol_file_missing() -> None:
    """When the file doesn't exist on disk, source is None with an explanatory note."""
    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        repo.mkdir()
        node = make_symbol(
            "pkg.foo",
            signature="def foo(x):",
            unit="proj/nonexistent.py",
            location=Location(path="proj/nonexistent.py", start_line=3, end_line=6),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/nonexistent.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("foo")

        assert result["found"] is True
        assert result["source"] is None
        assert "note" in result
        assert "source file not found" in result["note"].lower()


def test_read_symbol_truncated() -> None:
    """When source exceeds MAX_SOURCE_LINES, it is truncated and truncated=True."""
    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        repo.mkdir()
        # Create a file with many lines
        file_path = repo / "big.py"
        lines = [f"# line {i}\n" for i in range(1, 200)]
        file_path.write_text("".join(lines))

        # Node spans lines that would exceed the cap
        node = make_symbol(
            "pkg.big_fn",
            signature="def big_fn():",
            unit="proj/big.py",
            location=Location(path="proj/big.py", start_line=1, end_line=150),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/big.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("big_fn")

        assert result["found"] is True
        assert result["truncated"] is True
        # Source should be capped at MAX_SOURCE_LINES
        source_lines = result["source"].split("\n")
        # Allow for some variation but should be around the cap
        assert len(source_lines) <= 130  # Cap is 120 plus some buffer


def test_read_symbol_single_line() -> None:
    """A single-line symbol reads correctly."""
    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        repo.mkdir()
        file_path = repo / "single.py"
        file_content = "x = 1\n"
        file_path.write_text(file_content)

        node = make_symbol(
            "pkg.x",
            signature=None,
            unit="proj/single.py",
            location=Location(path="proj/single.py", start_line=1, end_line=1),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/single.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("x")

        assert result["found"] is True
        assert result["truncated"] is False
        assert result["source"] == "x = 1"


def test_read_symbol_crlf_no_trailing_carriage_return() -> None:
    """A CRLF-encoded file reads clean: read_text()'s universal-newline mode normalizes
    '\\r\\n' to '\\n', so no carriage returns survive onto the returned lines."""
    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        repo.mkdir()
        file_path = repo / "crlf.py"
        # Write with explicit CRLF line endings (Windows-authored source).
        file_path.write_bytes(b"def foo():\r\n    return 1\r\n")

        node = make_symbol(
            "pkg.foo",
            signature="def foo():",
            unit="proj/crlf.py",
            location=Location(path="proj/crlf.py", start_line=1, end_line=2),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/crlf.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("foo")

        assert result["source"] == "def foo():\n    return 1"
        assert "\r" not in result["source"]


def test_read_symbol_end_to_end_via_real_pipeline() -> None:
    """End-to-end guard: index a real tmp repo with the actual extract pipeline, then
    read a symbol's source. This pins the on-disk path convention (base = root.parent)
    against the real indexer, so a fixture/pipeline mismatch can never hide again."""
    from cartogate.extract.pipeline import index_package

    with TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "proj"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text(
            "def greet(name):\n    return f'hi {name}'\n"
        )
        store = InMemoryStore()
        index_package(repo, repo_id="proj", store=store)

        result = CartogateTools(store, root=repo).read_symbol("greet")

        assert result["found"] is True
        assert result["source"] is not None, result.get("note")
        assert "return f'hi {name}'" in result["source"]


def test_read_symbol_absolute_path_is_contained() -> None:
    """Defense-in-depth: an absolute location.path must not escape the workspace.

    A Path join silently discards the base when the right side is absolute, so without a
    containment guard a non-conforming/absolute path would read an arbitrary local file.
    """
    with TemporaryDirectory() as tmpdir:
        # Nest the repo so the index base (root.parent = tmpdir/workspace) does NOT contain
        # the secret, which lives at tmpdir/secret.txt — a genuine escape target.
        repo = Path(tmpdir) / "workspace" / "proj"
        repo.mkdir(parents=True)
        secret = Path(tmpdir) / "secret.txt"
        secret.write_text("TOP SECRET\n")

        node = make_symbol(
            "pkg.evil",
            signature=None,
            unit="proj/evil.py",
            location=Location(path=str(secret), start_line=1, end_line=1),
        )
        store = InMemoryStore()
        store.upsert_unit("proj/evil.py", [node], [])

        result = CartogateTools(store, root=repo).read_symbol("evil")

        assert result["found"] is True
        assert result["source"] is None
        assert "TOP SECRET" not in (result.get("source") or "")
        assert "outside workspace" in result["note"].lower()
