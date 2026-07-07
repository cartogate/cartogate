"""Architecture gate: module-level dependency-cycle detection (F-11).

A circular dependency is invisible to a single-file AST view — it only exists on the whole-program
graph — which is exactly what makes it a CPG-native check (impossible for a grep or a one-file
linter). These tests index a small package and assert cycles are found (and not invented).
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.architecture import find_cycles
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def _index(tmp_path: Path, files: dict[str, str]) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def test_detects_a_two_module_import_cycle(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "a.py": "from pkg.b import beta\n\n\ndef alpha():\n    return beta()\n",
            "b.py": "from pkg.a import alpha\n\n\ndef beta():\n    return 1\n",
        },
    )
    cycles = find_cycles(store)
    assert any(set(c) == {"pkg.a", "pkg.b"} for c in cycles)


def test_no_cycle_in_an_acyclic_package(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "a.py": "def alpha():\n    return 1\n",
            "b.py": "from pkg.a import alpha\n\n\ndef beta():\n    return alpha()\n",
        },
    )
    assert find_cycles(store) == []


def test_cycle_output_is_deterministic_and_canonical(tmp_path: Path) -> None:
    # A 3-module cycle a -> b -> c -> a; the reported cycle starts at the smallest module name
    # and the result is stable across runs.
    store = _index(
        tmp_path,
        {
            "a.py": "from pkg.b import beta\n\n\ndef alpha():\n    return beta()\n",
            "b.py": "from pkg.c import gamma\n\n\ndef beta():\n    return gamma()\n",
            "c.py": "from pkg.a import alpha\n\n\ndef gamma():\n    return alpha()\n",
        },
    )
    cycles = find_cycles(store)
    assert cycles == [["pkg.a", "pkg.b", "pkg.c"]]
    assert find_cycles(store) == cycles  # stable


def test_cycle_longer_than_the_length_bound_is_still_reported(tmp_path: Path) -> None:
    # A pure 3-module cycle with no shorter sub-cycle. With length_bound=2 the bounded enumeration
    # finds nothing, but the SCC-level fallback must still surface the circular dependency (no real
    # cycle is ever silently dropped — only the precise path is coarsened to the component members).
    store = _index(
        tmp_path,
        {
            "a.py": "from pkg.b import beta\n\n\ndef alpha():\n    return beta()\n",
            "b.py": "from pkg.c import gamma\n\n\ndef beta():\n    return gamma()\n",
            "c.py": "from pkg.a import alpha\n\n\ndef gamma():\n    return alpha()\n",
        },
    )
    cycles = find_cycles(store, length_bound=2)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"pkg.a", "pkg.b", "pkg.c"}  # the whole cyclic component, surfaced


def test_mixed_scc_short_and_long_cycles(tmp_path: Path) -> None:
    # One SCC {a,b,c,d}: a<->b is a 2-cycle; c and d sit only on the longer a->c->d->b->a (length-4)
    # cycle. This is the documented bound caveat: when a tiny bound finds the short cycle, modules
    # that appear ONLY on the over-bound cycle can be absent — but the SCC itself is still surfaced.
    store = _index(
        tmp_path,
        {
            "a.py": "from pkg.b import beta\nfrom pkg.c import gamma\n\n\ndef alpha():\n"
            "    return beta() + gamma()\n",
            "b.py": "from pkg.a import alpha\n\n\ndef beta():\n    return 1\n",
            "c.py": "from pkg.d import delta\n\n\ndef gamma():\n    return delta()\n",
            "d.py": "from pkg.b import beta\n\n\ndef delta():\n    return beta()\n",
        },
    )
    # Default bound (16): the length-4 cycle is enumerated, so c and d ARE covered.
    covered = {m for cyc in find_cycles(store) for m in cyc}
    assert {"pkg.a", "pkg.b", "pkg.c", "pkg.d"} <= covered
    # Tiny bound=2 (the gap): the short 2-cycle is reported; c/d (only on the length-4 cycle) are
    # absent — but pkg.a/pkg.b surface the cyclic component, so the SCC is never silently dropped.
    flat = {m for cyc in find_cycles(store, length_bound=2) for m in cyc}
    assert {"pkg.a", "pkg.b"} <= flat
    assert "pkg.c" not in flat and "pkg.d" not in flat
