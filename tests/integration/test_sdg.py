"""Interprocedural backward slicing (SDG, F-03) over a really-indexed package."""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.sdg import interprocedural_backward_slice
from cartogate.extract.pipeline import index_package
from cartogate.slice_cli import main as slice_main
from cartogate.store import InMemoryStore

# caller -> helper -> leaf ; the seed is a value in `caller` built from helper(...)'s result.
_CALC = (
    "def leaf(z):\n"  # 1
    "    return z * 2\n"  # 2
    "def helper(b):\n"  # 3
    "    return leaf(b) + 1\n"  # 4
    "def caller(x):\n"  # 5
    "    noise = 99\n"  # 6  (dead store — must not appear in the slice)
    "    y = helper(x)\n"  # 7  (calls helper; flows to the return)
    "    return y\n"  # 8  <-- seed
)


def _index(tmp_path: Path) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calc.py").write_text(_CALC, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def _read(tmp_path: Path):
    def read(rel: str) -> bytes | None:
        try:
            return (tmp_path / rel).read_bytes()
        except OSError:
            return None

    return read


def test_interprocedural_slice_crosses_call_boundaries(tmp_path: Path) -> None:
    store = _index(tmp_path)
    result = interprocedural_backward_slice(
        store, _read(tmp_path), "pkg/calc.py", 8, depth=3
    )
    assert result is not None
    by_name = {f.qualified_name: f for f in result.functions}
    # the seed function and BOTH transitive callees are present
    assert "pkg.calc.caller" in by_name and by_name["pkg.calc.caller"].is_seed
    assert "pkg.calc.helper" in by_name  # 1 hop
    assert "pkg.calc.leaf" in by_name  # 2 hops (helper -> leaf)
    # the dead store in the seed function does not appear
    assert 6 not in by_name["pkg.calc.caller"].lines
    assert 7 in by_name["pkg.calc.caller"].lines  # y = helper(x) is in the slice


def test_depth_bounds_expansion(tmp_path: Path) -> None:
    store = _index(tmp_path)
    result = interprocedural_backward_slice(
        store, _read(tmp_path), "pkg/calc.py", 8, depth=1
    )
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert "pkg.calc.helper" in names  # 1 hop reached
    assert "pkg.calc.leaf" not in names  # 2 hops is beyond depth=1


def test_no_unrelated_function(tmp_path: Path) -> None:
    # A change-free callee that the seed does NOT call must not appear.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "m.py").write_text(
        "def used(a):\n    return a\n"
        "def unused(a):\n    return a\n"
        "def top(x):\n    return used(x)\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "pkg/m.py", 6, depth=3)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert "pkg.m.used" in names and "pkg.m.unused" not in names


def test_cli_interprocedural_json(tmp_path: Path, capsys) -> None:
    import json

    _index(tmp_path)
    rc = slice_main(
        ["pkg/calc.py:8", "--interprocedural", "--root", str(tmp_path), "--json"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    names = {f["qualified_name"] for f in json.loads(out)["functions"]}
    assert {"pkg.calc.caller", "pkg.calc.helper", "pkg.calc.leaf"} <= names


def test_cli_forward_interprocedural_rejected(capsys) -> None:
    rc = slice_main(["x.py:1", "--interprocedural", "--forward"])
    assert rc == 1 and "not supported with --interprocedural" in capsys.readouterr().err


def test_interprocedural_slice_go(tmp_path: Path) -> None:
    # The SDG follows Go call edges too: caller -> helper -> leaf within a package (F-08). Go's
    # package = directory, so the file lives in a package subdir (the dir name is the module).
    pkg = tmp_path / "calc"
    pkg.mkdir()
    (pkg / "calc.go").write_text(
        "package calc\n"
        "func leaf(z int) int {\n\treturn z * 2\n}\n"
        "func helper(b int) int {\n\treturn leaf(b) + 1\n}\n"
        "func caller(x int) int {\n\ty := helper(x)\n\treturn y\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="app", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "calc/calc.go", 9, depth=3)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert any(n.endswith("caller") for n in names)
    assert any(n.endswith("helper") for n in names)
    assert any(n.endswith("leaf") for n in names)


def test_interprocedural_slice_c(tmp_path: Path) -> None:
    # The SDG follows C call edges too: caller -> helper -> leaf (F-08). A C file is its own module.
    (tmp_path / "calc.c").write_text(
        "int leaf(int z) { return z * 2; }\n"
        "int helper(int b) { return leaf(b) + 1; }\n"
        "int caller(int x) {\n  int y = helper(x);\n  return y;\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="app", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "calc.c", 5, depth=3)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert any(n.endswith("caller") for n in names)
    assert any(n.endswith("helper") for n in names)
    assert any(n.endswith("leaf") for n in names)


def test_interprocedural_slice_java(tmp_path: Path) -> None:
    # The SDG follows Java call edges too: caller -> helper -> leaf (F-08). Package = directory.
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "App.java").write_text(
        "package app;\nclass App {\n"
        "  int leaf(int z) { return z * 2; }\n"
        "  int helper(int b) { return leaf(b) + 1; }\n"
        "  int caller(int x) {\n    int y = helper(x);\n    return y;\n  }\n"
        "}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="app", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "app/App.java", 7, depth=3)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert any(n.endswith("caller") for n in names)
    assert any(n.endswith("helper") for n in names)
    assert any(n.endswith("leaf") for n in names)


def test_interprocedural_slice_typescript(tmp_path: Path) -> None:
    # The SDG follows TS call edges too: caller -> helper -> leaf across .ts files (F-08).
    (tmp_path / "leaf.ts").write_text(
        "export function leaf(z: number) {\n  return z * 2;\n}\n", encoding="utf-8"
    )
    (tmp_path / "helper.ts").write_text(
        "import { leaf } from './leaf';\n"
        "export function helper(b: number) {\n  return leaf(b) + 1;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "main.ts").write_text(
        "import { helper } from './helper';\n"
        "export function caller(x: number) {\n  const y = helper(x);\n  return y;\n}\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="app", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "main.ts", 4, depth=3)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert any(n.endswith("caller") for n in names)
    assert any(n.endswith("helper") for n in names)  # 1 hop across files
    assert any(n.endswith("leaf") for n in names)  # 2 hops


def test_aliased_import_callee_is_followed(tmp_path: Path) -> None:
    # `from ... import leaf as lf; lf(x)` — the call name (lf) differs from the callee qname (leaf).
    # Matching by the resolved CALLS-edge line (not the source name) must still follow it (review).
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "leafmod.py").write_text("def leaf(z):\n    return z * 2\n", encoding="utf-8")
    (pkg / "use.py").write_text(
        "from pkg.leafmod import leaf as lf\n\ndef caller(x):\n    return lf(x)\n",
        encoding="utf-8",
    )
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    result = interprocedural_backward_slice(store, _read(tmp_path), "pkg/use.py", 4, depth=2)
    assert result is not None
    names = {f.qualified_name for f in result.functions}
    assert "pkg.leafmod.leaf" in names  # followed despite the import alias
