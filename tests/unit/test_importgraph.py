"""Lightweight Python module-import graph — the substrate for the new-cycle advisory.

Built structurally from import statements (tree-sitter; no name resolution), so the commit gate
can compare old-vs-new import topology in milliseconds. Edges point only at repo-internal
modules; externals are ignored. Deterministic by construction.
"""

from __future__ import annotations

from cartogate.importgraph import (
    build_import_graph,
    find_new_cycles,
    module_name_for,
    python_imports_in,
)


def test_module_name_for_paths() -> None:
    assert module_name_for("pkg/mod.py") == "pkg.mod"
    assert module_name_for("pkg/__init__.py") == "pkg"
    assert module_name_for("top.py") == "top"


def test_plain_and_from_imports() -> None:
    src = "import pkg.db\nfrom pkg.auth import login\nimport os\n"
    # from-imports emit CANDIDATES: pkg.auth and pkg.auth.login (login may be a module).
    assert python_imports_in(src, module="app") == [
        "os",
        "pkg.auth",
        "pkg.auth.login",
        "pkg.db",
    ]


def test_relative_imports_resolve_against_the_module() -> None:
    src = "from . import util\nfrom .sub import x\nfrom ..other import y\n"
    # In module pkg.inner.mod: "." -> pkg.inner, ".sub" -> pkg.inner.sub, "..other" -> pkg.other
    # (each with the imported-name candidate alongside).
    assert python_imports_in(src, module="pkg.inner.mod") == [
        "pkg.inner",
        "pkg.inner.sub",
        "pkg.inner.sub.x",
        "pkg.inner.util",
        "pkg.other",
        "pkg.other.y",
    ]


def test_type_checking_imports_are_not_edges() -> None:
    """`if TYPE_CHECKING:` imports exist to BREAK runtime cycles — never count them."""
    src = (
        "from typing import TYPE_CHECKING" + chr(10)
        + "if TYPE_CHECKING:" + chr(10)
        + "    from app import b" + chr(10)
        + "import os" + chr(10)
    )
    targets = python_imports_in(src, module="app.a")
    assert "app.b" not in targets and "app" not in targets
    assert "os" in targets  # imports OUTSIDE the guard still count


def test_build_graph_keeps_only_repo_internal_edges() -> None:
    files = {
        "app/a.py": "import app.b\nimport requests\n",
        "app/b.py": "from app import a\n",
    }
    graph = build_import_graph(files)
    assert graph == {"app.a": {"app.b"}, "app.b": {"app.a"}}


def test_from_import_falls_back_to_the_module_prefix() -> None:
    # "from app.b import helper" -> app.b.helper is not a module; the prefix app.b is.
    files = {
        "app/a.py": "from app.b import helper\n",
        "app/b.py": "",
    }
    assert build_import_graph(files) == {"app.a": {"app.b"}, "app.b": set()}


def test_package_init_relative_imports_resolve_at_package_level() -> None:
    """Review HIGH-3: in pkg/sub/__init__.py, `from . import x` means pkg.sub.x (the
    package IS its own dotted name), and `from .. import y` means pkg.y."""
    targets = python_imports_in(
        "from . import x" + chr(10) + "from .. import y" + chr(10),
        module="pkg.sub",
        is_package=True,
    )
    assert "pkg.sub.x" in targets and "pkg.y" in targets


def test_init_reexport_edges_land_in_the_graph() -> None:
    files = {
        "pkg/__init__.py": "from .sub import thing" + chr(10),
        "pkg/sub/__init__.py": "from . import util" + chr(10),
        "pkg/sub/util.py": "",
    }
    graph = build_import_graph(files)
    assert graph["pkg"] == {"pkg.sub"}
    assert graph["pkg.sub"] == {"pkg.sub.util"}


def test_src_layout_modules_keep_their_import_names() -> None:
    """Review HIGH-2: src-layout repos (this one included) import as pkg.x, never
    src.pkg.x — without stripping, every internal edge silently vanished."""
    assert module_name_for("src/cartogate/precommit.py") == "cartogate.precommit"
    files = {
        "src/app/a.py": "import app.b" + chr(10),
        "src/app/b.py": "import app.a" + chr(10),
    }
    assert find_new_cycles({}, build_import_graph(files)) == [["app.a", "app.b"]]


def test_find_new_cycles_reports_only_introduced_ones() -> None:
    old = {"a": {"b"}, "b": set(), "c": {"d"}, "d": {"c"}}  # c<->d pre-exists
    new = {"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}}  # b->a is new
    cycles = find_new_cycles(old, new)
    assert cycles == [["a", "b"]]  # canonicalized, and c<->d NOT re-accused


def test_no_new_cycles_when_editing_inside_an_existing_one() -> None:
    old = {"a": {"b"}, "b": {"a"}}
    new = {"a": {"b"}, "b": {"a"}}
    assert find_new_cycles(old, new) == []


def test_determinism() -> None:
    old: dict[str, set[str]] = {"x": set(), "y": set(), "z": set()}
    new = {"x": {"y"}, "y": {"z"}, "z": {"x"}}
    assert find_new_cycles(old, new) == find_new_cycles(old, new) == [["x", "y", "z"]]
