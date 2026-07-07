"""LOCALIZE (F-02) — rank likely culprits behind a failing test (graph reach ∩ change)."""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.diff import parse_unified_diff
from cartogate.engine.localize import localize, refine_with_cfg, refine_with_pdg
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.store.base import FileRegion


def _index(tmp_path: Path, files: dict[str, str]) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


_FILES = {
    # test -> run() -> helper(); only helper changed.
    "core.py": (
        "def helper():\n    return 1\n\n\n"
        "def run():\n    return helper()\n\n\n"
        "def untouched():\n    return 99\n"
    ),
    "test_core.py": "from pkg.core import run\n\ndef test_run():\n    assert run() == 1\n",
}


def _diff_touching_helper() -> str:
    # A unified diff changing line 2 (helper's body).
    return (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
        "@@ -2 +2 @@\n-    return 1\n+    return 2\n"
    )


def test_ranks_changed_symbol_in_test_reach(tmp_path: Path) -> None:
    store = _index(tmp_path, _FILES)
    report = localize(store, "pkg.test_core.test_run", parse_unified_diff(_diff_touching_helper()))
    data = report.to_dict()
    assert data["found"] is True
    suspects = [s["qualified_name"] for s in data["suspects"]]
    assert "pkg.core.helper" in suspects  # changed AND exercised by the test -> a suspect
    assert "pkg.core.untouched" not in suspects  # not changed -> not a suspect
    assert "pkg.core.run" not in suspects  # reachable (distance 1) but NOT changed -> excluded
    # helper is reached via run() -> distance 2 from the test.
    helper = next(s for s in data["suspects"] if s["qualified_name"] == "pkg.core.helper")
    assert helper["distance"] == 2


def test_no_suspects_when_change_outside_reach(tmp_path: Path) -> None:
    store = _index(tmp_path, _FILES)
    # A diff touching `untouched` (line 9) — not in test_run's reach.
    diff = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n"
        "@@ -9 +9 @@\n-    return 99\n+    return 98\n"
    )
    report = localize(store, "pkg.test_core.test_run", parse_unified_diff(diff))
    assert report.found is True
    assert report.suspects == ()
    assert "cause may be elsewhere" in report.to_markdown()


def test_unknown_test_is_reported(tmp_path: Path) -> None:
    store = _index(tmp_path, _FILES)
    report = localize(store, "pkg.core.nope", parse_unified_diff(_diff_touching_helper()))
    assert report.found is False
    assert "not in the graph" in report.to_dict()["reason"]


def test_deterministic(tmp_path: Path) -> None:
    store = _index(tmp_path, _FILES)
    regions = parse_unified_diff(_diff_touching_helper())
    first = localize(store, "pkg.test_core.test_run", regions)
    assert first.to_dict() == localize(store, "pkg.test_core.test_run", regions).to_dict()


# --- statement-level refinement (F-03 / CFG) ---------------------------------------------------- #

_REFINE_SRC = (
    "def helper(x):\n"  # line 1: def
    "    if x:\n"  # 2
    "        return live(x)\n"  # 3: reachable, calls live
    "    return 0\n"  # 4
    "    dead_call(x)\n"  # 5: unreachable (after return)
    "def live(x):\n    return x\n"
    "def dead_call(x):\n    return x\n"
)


def _refine_store(tmp_path: Path):
    return _index(
        tmp_path,
        {"core.py": _REFINE_SRC, "t.py": "from pkg.core import helper\n"
         "def test_h():\n    assert helper(1)\n"},
    )


def _read_core(path: str) -> bytes | None:
    return _REFINE_SRC.encode() if path == "pkg/core.py" else None


def test_refine_drops_dead_code_change(tmp_path: Path) -> None:
    # `helper` changed only at line 5 (unreachable, `dead_call(x)` after the returns). The change is
    # confined to dead code, so `helper` — a suspect before refinement — must be DROPPED.
    store = _refine_store(tmp_path)
    regions = [FileRegion("pkg/core.py", 5, 5)]
    report = localize(store, "pkg.t.test_h", regions, max_depth=5)
    assert "pkg.core.helper" in {s.qualified_name for s in report.suspects}  # a suspect pre-refine
    refined = refine_with_cfg(report, regions, _read_core)
    assert "pkg.core.helper" not in {s.qualified_name for s in refined.suspects}  # dead -> dropped


def test_refine_keeps_signature_change_even_with_dead_line(tmp_path: Path) -> None:
    # Soundness (review HIGH): a change touching BOTH the def line (line 1, behaviour-affecting) and
    # a dead statement (line 5) must NOT be dropped — only a change confined to dead code may drop.
    store = _refine_store(tmp_path)
    regions = [FileRegion("pkg/core.py", 1, 1), FileRegion("pkg/core.py", 5, 5)]
    report = localize(store, "pkg.t.test_h", regions, max_depth=5)
    refined = refine_with_cfg(report, regions, _read_core)
    assert "pkg.core.helper" in {s.qualified_name for s in refined.suspects}  # kept (def changed)


def test_refine_annotates_reachable_changed_line(tmp_path: Path) -> None:
    # A change to line 3 (reachable, calls `live`) is kept and annotated with the changed line.
    store = _refine_store(tmp_path)
    regions = [FileRegion("pkg/core.py", 3, 3)]
    report = localize(store, "pkg.t.test_h", regions, max_depth=5)
    refined = refine_with_cfg(report, regions, _read_core)
    helper = next((s for s in refined.suspects if s.qualified_name == "pkg.core.helper"), None)
    assert helper is not None and 3 in helper.changed_lines  # reachable changed line surfaced


# --- PDG output-slice refinement (F-03 / PDG) --------------------------------------------------- #

_PDG_SRC = (
    "def calc(x):\n"  # 1: def
    "    note = 'log'\n"  # 2: dead store (note is never used) -> no observable effect
    "    y = x + 1\n"  # 3: flows into the return
    "    return double(y)\n"  # 4: return + call -> observable output
    "def double(v):\n    return v * 2\n"
)


def _pdg_store(tmp_path: Path):
    return _index(
        tmp_path,
        {"calc.py": _PDG_SRC, "t.py": "from pkg.calc import calc\n"
         "def test_c():\n    assert calc(1)\n"},
    )


def _read_calc(path: str) -> bytes | None:
    return _PDG_SRC.encode() if path == "pkg/calc.py" else None


def test_pdg_flags_output_relevant_changed_line(tmp_path: Path) -> None:
    # A change to line 3 (`y = x + 1`) flows into `return double(y)` -> output-relevant.
    store = _pdg_store(tmp_path)
    regions = [FileRegion("pkg/calc.py", 3, 3)]
    report = localize(store, "pkg.t.test_c", regions, max_depth=5)
    refined = refine_with_pdg(refine_with_cfg(report, regions, _read_calc), regions, _read_calc)
    calc = next(s for s in refined.suspects if s.qualified_name == "pkg.calc.calc")
    assert calc.output_analyzed is True
    assert 3 in calc.output_relevant_lines  # the change reaches the return


def test_pdg_flags_no_observable_effect_but_keeps_suspect(tmp_path: Path) -> None:
    # A change to line 2 (`note = 'log'`, a dead store) reaches no observable output. It must be
    # flagged (output_relevant empty) but NEVER dropped — soundness: a missed effect can't hide it.
    store = _pdg_store(tmp_path)
    regions = [FileRegion("pkg/calc.py", 2, 2)]
    report = localize(store, "pkg.t.test_c", regions, max_depth=5)
    refined = refine_with_pdg(refine_with_cfg(report, regions, _read_calc), regions, _read_calc)
    calc = next(s for s in refined.suspects if s.qualified_name == "pkg.calc.calc")  # still present
    assert calc.output_analyzed is True and calc.output_relevant_lines == ()
    assert "no observable effect found" in refined.to_markdown()


_RANK_SRC = (
    "def aaa(x):\n"  # 1
    "    note = 'x'\n"  # 2: dead store -> no observable effect
    "    return 0\n"  # 3
    "def bbb(x):\n"  # 4
    "    return use(x)\n"  # 5: flows to output
    "def use(v):\n    return v\n"
)


def test_pdg_soft_ranks_output_relevant_first(tmp_path: Path) -> None:
    # aaa and bbb are both directly exercised (distance 1) and both changed; aaa's change is a no-op
    # while bbb's reaches output -> bbb sorts first despite 'aaa' < 'bbb' alphabetically.
    store = _index(
        tmp_path,
        {"r.py": _RANK_SRC, "t.py": "from pkg.r import aaa, bbb\n"
         "def test_r():\n    assert aaa(1) == bbb(1)\n"},
    )

    def _read(path: str) -> bytes | None:
        return _RANK_SRC.encode() if path == "pkg/r.py" else None

    regions = [FileRegion("pkg/r.py", 2, 2), FileRegion("pkg/r.py", 5, 5)]
    report = localize(store, "pkg.t.test_r", regions, max_depth=5)
    refined = refine_with_pdg(refine_with_cfg(report, regions, _read), regions, _read)
    names = [s.qualified_name for s in refined.suspects]
    assert names.index("pkg.r.bbb") < names.index("pkg.r.aaa")  # output-relevant ranks first
