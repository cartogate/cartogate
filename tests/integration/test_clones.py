"""Near-duplicate body detection (F-32) — catch copy-pasted function bodies.

The duplicate *gate* is signature-exact (and top-level only), so it misses a function that was
copied and given a new name but kept the same body. This advisory check flags functions whose
*normalized body is identical* — a verbatim copy-paste, even across a rename — above a size floor
that keeps trivial one-liners out. High-confidence (identical body) and never blocks.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.clones import find_duplicate_bodies
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore

_BODY = (
    "    total = 0\n"
    "    for item in items:\n"
    "        total += item.price\n"
    "    return total\n"
)


def _index(tmp_path: Path, files: dict[str, str]) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def test_detects_copy_pasted_function_across_a_rename(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "a.py": f"def sum_prices(items):\n{_BODY}",
            "b.py": f"def total_cost(items):\n{_BODY}",  # same body, renamed function
            "c.py": "def unique(x):\n    return x * 2 + 1\n",  # a different body
        },
    )
    clones = find_duplicate_bodies(store, min_lines=3)
    assert any(set(g) == {"pkg.a.sum_prices", "pkg.b.total_cost"} for g in clones)
    assert not any("pkg.c.unique" in g for g in clones)  # no twin -> not a clone


def test_ignores_trivial_bodies_below_the_size_floor(tmp_path: Path) -> None:
    # Two one-line getters with the same body are not worth flagging (noise, not copy-paste debt).
    store = _index(
        tmp_path,
        {
            "a.py": "def name(self):\n    return self._name\n",
            "b.py": "def label(self):\n    return self._name\n",
        },
    )
    assert find_duplicate_bodies(store, min_lines=3) == []


def test_detects_copy_pasted_arrow_consts_in_typescript(tmp_path: Path) -> None:
    # Arrow-const functions are a dominant JS/TS pattern; their body lives on the arrow node, not
    # the declarator — confirm the walker still hashes it so copies are caught.
    proj = tmp_path / "proj"
    proj.mkdir()
    arrow = (
        "(items: number[]): number => {\n"
        "  let total = 0;\n"
        "  for (const n of items) total += n;\n"
        "  return total;\n"
        "}"
    )
    (proj / "a.ts").write_text(f"export const sumAll = {arrow};\n", encoding="utf-8")
    (proj / "b.ts").write_text(f"export const addUp = {arrow};\n", encoding="utf-8")
    store = InMemoryStore()
    index_package(proj, repo_id="proj", store=store, index_docs=False)
    clones = find_duplicate_bodies(store, min_lines=3)
    assert any(set(g) == {"proj.a.sumAll", "proj.b.addUp"} for g in clones)


def test_whitespace_and_formatting_insensitive_but_deterministic(tmp_path: Path) -> None:
    # Re-indented / reformatted copies still match (normalized body), and output is stable.
    reindented = (
        "    total = 0\n"
        "    for item in items:\n"
        "            total += item.price\n"  # different indentation
        "    return total\n"
    )
    store = _index(
        tmp_path,
        {
            "a.py": f"def sum_prices(items):\n{_BODY}",
            "b.py": f"def total_cost(items):\n{reindented}",
        },
    )
    clones = find_duplicate_bodies(store, min_lines=3)
    assert clones == [["pkg.a.sum_prices", "pkg.b.total_cost"]]
    assert find_duplicate_bodies(store, min_lines=3) == clones  # deterministic
