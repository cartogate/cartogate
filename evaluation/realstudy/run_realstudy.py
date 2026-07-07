"""Real-repo, independent-oracle study — writes the headline V3/V4/V7 rows.

Orchestrates: fetch the pinned corpus → build a flat workspace → index it with Cartogate →
score navigation against **pyright** (V3), the duplicate gate by **construction** (V4), and
test selection against **coverage.py** (V7). Results are merged into
``evaluation/value_results.json
(the recorded results)``
under the headline ids ``V3``/``V4``/``V7`` (the self-authored fixture keeps the ``*_fixture``
ids). Opt-in — needs Node/pyright and runs the corpus's own test suite; **not** in CI.

Usage:
    python -m evaluation.realstudy.run_realstudy --sample 25
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from cartogate.schema.enums import NodeKind

from ..corpus.fetch_corpus import CORPORA, DEFAULT_CORPUS, Corpus, fetch, tests_dir
from . import inject_dupes
from .coverage_tests import CoverageOracle, cartogate_test_id
from .pyright_refs import PyrightReferences
from .scoring import Score, add, score
from .workspace import Workspace, build_workspace

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_PATH = REPO_ROOT / "docs" / "value_results.json"


def _reproduce(corpus: Corpus) -> str:
    return f"python -m evaluation.realstudy.run_realstudy --corpus {corpus.name} --sample 25"


# --------------------------------------------------------------------------- #
# V3 — navigation vs pyright
# --------------------------------------------------------------------------- #


def _grep_ref_units(package_dir: Path, name: str, package: str) -> set[str]:
    word = re.compile(rf"\b{re.escape(name)}\b")
    out: set[str] = set()
    for path in sorted(package_dir.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if word.search(text):
            out.add(f"{package}/{path.relative_to(package_dir).as_posix()}")
    return out


def run_v3(ws: Workspace, sample: int, corpus: Corpus, suffix: str) -> dict[str, Any]:
    candidates = sorted(
        (n for n in ws.nodes
         if n.kind is NodeKind.SYMBOL and n.is_top_level and n.signature
         and not n.name.startswith("_")),
        key=lambda n: n.qualified_name,
    )
    gg_total, grep_total = Score(0, 0, 0), Score(0, 0, 0)
    per_symbol: list[dict[str, Any]] = []
    with PyrightReferences(ws.root, ws.package_dir, settle=12.0) as oracle:
        for node in candidates:
            if len(per_symbol) >= sample:
                break
            truth = oracle.references(node.unit, node.location.start_line)
            if not truth:  # nothing references it → no meaningful precision/recall
                continue
            gg = {r["unit"] for r in ws.tools.find_references(node.qualified_name)["references"]}
            grep = _grep_ref_units(ws.package_dir, node.name, ws.package)
            gg_s, grep_s = score(gg, truth), score(grep, truth)
            gg_total, grep_total = add(gg_total, gg_s), add(grep_total, grep_s)
            per_symbol.append({
                "symbol": node.qualified_name,
                "truth_files": len(truth),
                "cartogate": gg_s.to_dict(),
                "grep": grep_s.to_dict(),
            })
    return {
        "id": f"V3{suffix}",
        "bucket": "A",
        "title": "Navigation accuracy (find_references vs pyright oracle)",
        "claim": "Against pyright's references on a real repo, Cartogate matches with far higher "
        "precision than name-grep; its recall gap is the re-exports/inferred receivers it "
        "soundly skips.",
        "metric": {
            "oracle": "pyright (independent of jedi)",
            "corpus": f"{corpus.name} {corpus.tag}",
            "corpus_kind": corpus.kind,
            "symbols_scored": len(per_symbol),
            "cartogate": gg_total.to_dict(),
            "grep_baseline": grep_total.to_dict(),
            "per_symbol": per_symbol,
        },
        "passed": gg_total.precision > grep_total.precision,
        "reproduce": _reproduce(corpus),
        "notes": "Micro-averaged over scored symbols. File granularity (Cartogate returns "
        "referencing symbols, pyright returns occurrences).",
    }


# --------------------------------------------------------------------------- #
# V7 — test selection vs coverage
# --------------------------------------------------------------------------- #


def run_v7(
    ws: Workspace, sample: int, max_truth: int, corpus: Corpus, suffix: str
) -> dict[str, Any]:
    oracle = CoverageOracle(ws.root, ws.package).run()
    candidates = sorted(
        (n for n in ws.nodes
         if n.kind is NodeKind.SYMBOL and n.is_top_level and n.signature
         and n.unit.startswith(f"{ws.package}/")),
        key=lambda n: n.qualified_name,
    )
    gg_total = Score(0, 0, 0)
    # Static-reachability census: split the runtime truth into the tests that have ANY static
    # path to the symbol (the ceiling for a *sound* static selector) and the tests reachable only
    # through dynamic dispatch (getattr tables, `runner.invoke`, registries) that no static
    # analyzer can follow. `reachable` is suggest_tests at a deep traversal — Cartogate's best
    # static effort — so `truth ∩ reachable` is what selection could ever recover.
    static_depth = 25
    curve_depths = (1, 2, 3, 4)

    def _suggest(qn: str, depth: int) -> set[str]:
        rep = ws.tools.suggest_tests(symbols=[qn], depth=depth)
        return {cartogate_test_id(t["qualified_name"], t["unit"]) for t in rep["tests"]}

    truth_total = reachable_truth_total = tp_total = 0
    curve_totals = {d: Score(0, 0, 0) for d in curve_depths}
    per_symbol: list[dict[str, Any]] = []
    for node in candidates:
        if len(per_symbol) >= sample:
            break
        abs_file = (ws.root / node.unit).resolve()
        truth = oracle.tests_covering(abs_file, node.location.start_line, node.location.end_line)
        if not (1 <= len(truth) <= max_truth):  # focus where selection is meaningful
            continue
        # depth=1 (the tool's default): the directly-referencing tests.
        selected = _suggest(node.qualified_name, 1)
        reachable = _suggest(node.qualified_name, static_depth)  # Cartogate's best static effort
        s = score(selected, truth)
        gg_total = add(gg_total, s)
        for d in curve_depths:
            curve_totals[d] = add(curve_totals[d], score(_suggest(node.qualified_name, d), truth))
        truth_total += len(truth)
        reachable_truth_total += len(truth & reachable)
        tp_total += s.tp
        per_symbol.append({
            "symbol": node.qualified_name,
            "truth_tests": len(truth),
            "selected_tests": len(selected),
            "statically_reachable_truth": len(truth & reachable),
            "score": s.to_dict(),
        })
    # Fraction of runtime test-coverage that any sound static selector could reach, and Cartogate's
    # recall measured only over that reachable set (≈1.0 when the gap is purely dynamic dispatch).
    reach_fraction = round(reachable_truth_total / truth_total, 4) if truth_total else 0.0
    recall_reachable = round(tp_total / reachable_truth_total, 4) if reachable_truth_total else 0.0
    # Depth curve: recall rises with traversal depth, but precision falls — recovering the deep
    # static chains costs the precision that makes selection useful, so depth=1 is the default.
    depth_curve = {str(d): curve_totals[d].to_dict() for d in curve_depths}
    notes = (
        "Truth = tests whose coverage executed the symbol's lines (transitive). Overall recall is "
        "low where coverage is dominated by dynamic dispatch (getattr tables, runner.invoke): only "
        f"~{reach_fraction:.0%} of these symbols' runtime coverage is statically reachable, but "
        f"Cartogate recovers ~{recall_reachable:.0%} of that reachable set."
    )
    return {
        "id": f"V7{suffix}",
        "bucket": "B",
        "title": "Test selection (suggest_tests vs coverage oracle)",
        "claim": "When suggest_tests names a test, coverage confirms that test really does "
        "exercise the symbol (high precision). Overall recall against full runtime coverage is "
        "bounded by how much of that coverage is statically reachable at all: Cartogate recovers "
        "≈all of the statically-reachable tests, and the rest is dynamic dispatch no sound static "
        "analyzer can follow.",
        "metric": {
            "oracle": "coverage.py per-test contexts (runtime truth)",
            "corpus": f"{corpus.name} {corpus.tag}",
            "corpus_kind": corpus.kind,
            "depth": 1,
            "suite_total_tests": oracle.total_tests,
            "pytest_summary": oracle.summary,
            "symbols_scored": len(per_symbol),
            "cartogate": gg_total.to_dict(),
            "static_reachability": {
                # reachable_fraction: of the runtime truth, the share reachable by ANY static path
                # (the ceiling for a sound selector; the rest is dynamic dispatch).
                # recall_within_reachable: Cartogate's recall over that reachable set (≈1.0 means
                # the gap is dispatch, not a Cartogate miss).
                "reachable_fraction": reach_fraction,
                "recall_within_reachable": recall_reachable,
                "static_depth": static_depth,
                # Recall climbs with traversal depth but precision collapses (deep static chains
                # rarely execute the symbol) — so the reachable recall isn't soundly usable.
                "depth_curve": depth_curve,
            },
            "per_symbol": per_symbol,
        },
        # The tool's claim is "the tests it names exercise the change" — judged on precision.
        "passed": gg_total.precision >= 0.7,
        "reproduce": _reproduce(corpus),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# V4 — duplicate gate by construction
# --------------------------------------------------------------------------- #


def run_v4(ws: Workspace, sample: int, corpus: Corpus, suffix: str) -> dict[str, Any]:
    data = inject_dupes.evaluate(ws.tools, ws.nodes, ws.package_dir, sample=sample)
    return {
        "id": f"V4{suffix}",
        "bucket": "B",
        "title": "Duplicate detection (check_duplicate vs name-grep, real symbols)",
        "claim": "On objectively-labeled cases built from real symbols, the gate catches true "
        "duplicates without false-flagging different-signature or method-named functions; "
        "name-grep over-blocks both.",
        "metric": {"corpus": f"{corpus.name} {corpus.tag}", "corpus_kind": corpus.kind, **data},
        "passed": data["cartogate"]["precision"] >= data["grep_baseline"]["precision"],
        "reproduce": _reproduce(corpus),
        "notes": "Labels are objective by construction (extra-param and method cases are not "
        "top-level duplicates). Census reports real signature collisions.",
    }


# --------------------------------------------------------------------------- #


def _merge_results(rows: list[dict[str, Any]]) -> None:
    data: dict[str, Any] = {"schema_version": 1, "hypotheses": {}}
    if RESULTS_PATH.exists():
        data = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    data.setdefault("hypotheses", {})
    for row in rows:
        data["hypotheses"][row["id"]] = row
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-repo, independent-oracle value study.")
    parser.add_argument("--corpus", choices=sorted(CORPORA), default=DEFAULT_CORPUS,
                        help="Which pinned corpus to study (default: click).")
    parser.add_argument("--sample", type=int, default=25, help="Symbols per hypothesis.")
    parser.add_argument("--max-truth", type=int, default=25,
                        help="V7: max exercising-tests for a symbol to be sampled.")
    parser.add_argument("--only", choices=["v3", "v4", "v7"], action="append",
                        help="Run only these hypotheses (default: all).")
    args = parser.parse_args()

    corpus = CORPORA[args.corpus]
    # The default corpus owns the headline ids (V3/V4/V7); others are suffixed (V3_jmespath, …),
    # mirroring the *_fixture convention, so a second corpus is additive (never overwrites click).
    suffix = "" if args.corpus == DEFAULT_CORPUS else f"_{args.corpus}"
    pkg = fetch(args.corpus)
    ws = build_workspace(pkg, tests_dir(args.corpus), corpus.package)
    only = set(args.only or ["v3", "v4", "v7"])
    rows: list[dict[str, Any]] = []
    try:
        if "v4" in only:
            rows.append(run_v4(ws, args.sample, corpus, suffix))
        if "v7" in only:
            rows.append(run_v7(ws, args.sample, args.max_truth, corpus, suffix))
        if "v3" in only:
            rows.append(run_v3(ws, args.sample, corpus, suffix))
    finally:
        ws.cleanup()

    _merge_results(rows)
    for row in rows:
        print(f"\n=== {row['id']} {row['title']} ===")
        print(json.dumps({k: v for k, v in row["metric"].items() if k != "per_symbol"
                          and k != "cases"}, indent=2))


if __name__ == "__main__":
    main()
