"""Installed git pre-commit gate — ``python -m cartogate.precommit`` (spec §7.4).

The agent-agnostic backstop: re-index the repo and refuse the commit if it contains duplicate
top-level function/class signatures (the same callable written twice). This lives *inside the
package* (not the repo's ``hooks/`` dir) so the hook ``cartogate init`` installs works in any
repo where Cartogate is installed — including a pipx install where ``hooks/`` isn't shipped.

Run from a git hook, the working directory is the repo root, so no argument is needed; a path
may be passed explicitly. Exit non-zero blocks the commit; the gate fails **closed** on any error.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

from cartogate.extract.pipeline import index_package
from cartogate.gitio import run_git
from cartogate.schema.enums import Language
from cartogate.schema.nodes import Node
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_duplicate_signatures

_ADVISORY_GIT_TIMEOUT_S = 10.0


def _staged_contract_changes(repo: Path) -> list[tuple[str, str, str, str]]:
    """``(path, qualified_name, old_sig, new_sig)`` for staged MODIFICATIONS that alter an
    established signature — extracted facts only (HEAD version vs staged version, walked
    identically and paired by qualified name).

    Latency: 2N+1 sequential git calls for N modified files — negligible on a healthy git;
    a wedged git bounds each call at the timeout. Known limitation noted for promotion review.
    """
    from cartogate.extract.languages import language_of, named_signatures_in
    from cartogate.schema.signature import normalize_signature

    listing = run_git(
        ["diff", "--cached", "--name-only", "--diff-filter=M"],
        cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
    )
    if listing is None:
        return []
    changes: list[tuple[str, str, str, str]] = []
    for path in sorted(listing.decode("utf-8", "replace").split()):
        language = language_of(path)
        if language is None:
            continue
        old_blob = run_git(["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        new_blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        if old_blob is None or new_blob is None:
            continue
        old_sigs = dict(named_signatures_in(old_blob.decode("utf-8", "replace"), language))
        for qname, new_sig in named_signatures_in(new_blob.decode("utf-8", "replace"), language):
            old_sig = old_sigs.get(qname)
            if old_sig is None or old_sig == new_sig:
                continue
            if normalize_signature(old_sig, language) != normalize_signature(new_sig, language):
                changes.append((path, qname.removeprefix("<snippet>."), old_sig, new_sig))
    return changes


def _staged_renames(repo: Path) -> list[tuple[str, str]] | None:
    """``(old_path, new_path)`` for staged RENAMES (git's similarity detection, default on).

    ``None`` on any failure or unparseable line (quoted/exotic paths) — the caller fails closed.
    """
    out = run_git(
        ["diff", "--cached", "--name-status", "--diff-filter=R"],
        cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
    )
    if out is None:
        return None
    pairs: list[tuple[str, str]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("	")
        if len(parts) != 3:
            return None  # can't trust the parse -> fail closed
        pairs.append((parts[1], parts[2]))
    return pairs


def _staged_new_symbols(repo: Path) -> set[tuple[str, str, str]] | None:
    """``(unit, qname, normalized_signature)`` for every symbol the staged diff ADDS or whose
    signature it CHANGES — the set the gate is allowed to judge (field bug, 2026-07-04: the gate
    refused a clean change over ~20 PRE-EXISTING duplicate groups it never touched).

    ``None`` means git could not answer (non-git dir, unborn-HEAD listing failure, wedged git) —
    the caller FAILS CLOSED and treats every duplicate group as introduced (original behavior).
    """
    from cartogate.extract.languages import language_of, named_signatures_in
    from cartogate.schema.signature import normalize_signature

    # --no-renames: a high-similarity rename is otherwise reported as R and would evade BOTH
    # filters — a duplicate introduced via a renamed file would falsely PASS (verified
    # empirically). Degraded to D+A, the renamed file's symbols are all judged as new.
    listing_a = run_git(
        ["diff", "--cached", "--no-renames", "--name-only", "--diff-filter=A"],
        cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
    )
    listing_m = run_git(
        ["diff", "--cached", "--no-renames", "--name-only", "--diff-filter=M"],
        cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
    )
    renames = _staged_renames(repo)
    if listing_a is None or listing_m is None or renames is None:
        return None
    # A renamed file appears as A under --no-renames; judged as a PAIR below instead, so a pure
    # `git mv` of a file with pre-existing duplicates doesn't false-block (review finding #2),
    # while a rename that smuggles a signature change still counts that symbol as new.
    rename_targets = {new_path for _, new_path in renames}
    new: set[tuple[str, str, str]] = set()

    def _symbols_of(path: str, blob: bytes, language: object) -> list[tuple[str, str]]:
        return [
            (qname.removeprefix("<snippet>."), normalize_signature(sig, language))  # type: ignore[arg-type]
            for qname, sig in named_signatures_in(blob.decode("utf-8", "replace"), language)  # type: ignore[arg-type]
        ]

    for path in sorted(listing_a.decode("utf-8", "replace").split()):
        language = language_of(path)
        if language is None or path in rename_targets:
            continue
        blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        if blob is None:
            return None  # can't judge -> fail closed
        unit = f"{repo.name}/{path}"
        for qname, nsig in _symbols_of(path, blob, language):
            new.add((unit, qname, nsig))
    for old_path, new_path in sorted(renames):
        language = language_of(new_path)
        if language is None:
            continue
        new_blob = run_git(["show", f":{new_path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        if new_blob is None:
            return None
        old_blob = run_git(
            ["show", f"HEAD:{old_path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
        )
        old = dict(_symbols_of(old_path, old_blob, language)) if old_blob is not None else {}
        unit = f"{repo.name}/{new_path}"
        for qname, nsig in _symbols_of(new_path, new_blob, language):
            if old.get(qname) != nsig:  # carried-over symbols are NOT new; changed ones are
                new.add((unit, qname, nsig))
    for path in sorted(listing_m.decode("utf-8", "replace").split()):
        language = language_of(path)
        if language is None:
            continue
        new_blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        if new_blob is None:
            return None
        old_blob = run_git(["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
        # A missing HEAD blob for an M file is unexpected — treat the file as all-new (the
        # fail-closed direction: more blocking, never less).
        old = dict(_symbols_of(path, old_blob, language)) if old_blob is not None else {}
        unit = f"{repo.name}/{path}"
        for qname, nsig in _symbols_of(path, new_blob, language):
            if old.get(qname) != nsig:
                new.add((unit, qname, nsig))
    return new


def _split_introduced(
    duplicates: dict[tuple[Language, str], list[Node]], new_symbols: set[tuple[str, str, str]]
) -> tuple[dict[tuple[Language, str], list[Node]], int]:
    """(groups this commit INTRODUCES, count of pre-existing groups).

    A group blocks iff some member is a staged-new symbol: same unit, same normalized signature
    (the group key), and the member's qualified name ends with the staged symbol's dotted name.

    Assumptions (documented per review): the suffix match presumes two symbols in the SAME unit
    with the SAME normalized signature and a shared dotted suffix are the same symbol; and unit
    strings compare case-sensitively (git's casing vs the walker's — on a case-insensitive FS a
    divergence would misclassify as pre-existing; known limitation).
    """
    by_unit: dict[str, list[tuple[str, str]]] = {}
    for unit, qname, nsig in new_symbols:
        by_unit.setdefault(unit, []).append((qname, nsig))

    def _introduced(nsig: str, members: list[Node]) -> bool:
        for m in members:
            for qname, staged_nsig in by_unit.get(m.unit, ()):
                if staged_nsig == nsig and (
                    m.qualified_name == qname or m.qualified_name.endswith("." + qname)
                ):
                    return True
        return False

    blocking = {
        key: members for key, members in duplicates.items() if _introduced(key[1], members)
    }
    return blocking, len(duplicates) - len(blocking)


def _is_test_path(path: str) -> bool:
    """Test-file heuristic over the staged path (POSIX from git): pytest naming or a tests dir.

    Fixture/sample trees are excluded — files under a ``fixtures`` dir are code to be ANALYZED
    (this repo's own tests/value/fixtures/**/test_*.py would otherwise false-fire).
    """
    if "/fixtures/" in f"/{path}/":
        return False
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "/tests/" in f"/{path}"
    )


def _test_metrics(source: str) -> tuple[int, int, int, int]:
    """``(assertions, skip_markers, test_functions, parametrize_markers)`` for a Python test
    file — tree-sitter precise for assertions (bare ``assert`` + unittest ``self.assert*``
    calls; no comment/string false counts), textual for the marker names.

    Blind spots (documented for the promotion review): weakening WITHOUT a count change
    (``assert x == 42`` -> ``assert x is not None``), ``pytest.raises`` removal (not an
    assert statement), and non-Python tests. This metric is a deterministic subset, not a
    completeness claim.
    """
    from tree_sitter import Parser

    from cartogate.extract.ast_walker import _PYTHON_LANGUAGE  # the shared compiled grammar
    from cartogate.extract.languages import named_signatures_in

    tree = Parser(_PYTHON_LANGUAGE).parse(source.encode("utf-8"))
    asserts = 0
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "assert_statement":
            asserts += 1
        elif node.type == "call":
            # unittest-style: self.assertEqual(...) etc. count as assertions too, so
            # unittest suites aren't invisible to the weakening metric.
            fn = node.child_by_field_name("function")
            if fn is not None and fn.text is not None and fn.text.startswith(b"self.assert"):
                asserts += 1
        stack.extend(node.named_children)
    markers = sum(
        source.count(marker)
        for marker in ("pytest.mark.skip", "pytest.mark.xfail", "unittest.skip")
    )
    test_functions = sum(
        1
        for qname, _sig in named_signatures_in(source)
        if qname.rsplit(".", 1)[-1].startswith("test_")
    )
    parametrize = source.count("pytest.mark.parametrize")
    return asserts, markers, test_functions, parametrize


def _print_cycle_advisory(repo: Path) -> None:
    """The new-cycle advisory (STRATEGY.md Phase 2): report import cycles THIS staged change
    introduces — architecture erosion is incremental, and each new cycle "looks right" locally.

    Diff-aware by construction: the OLD graph is the NEW graph with changed files' imports
    swapped for their HEAD versions, so a pre-existing cycle is never re-accused, even when the
    change edits a file inside it. Structural import statements only (no resolution — the
    resolver would take minutes on large repos at commit time); Python-only v1; advisory-only,
    never raises into the gate, never affects the exit code.
    """
    try:
        from cartogate.importgraph import build_import_graph, find_new_cycles

        listing = run_git(
            ["diff", "--cached", "--no-renames", "--name-status"],
            cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
        )
        if listing is None:
            return
        changed: dict[str, str] = {}
        for line in listing.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].endswith(".py"):
                changed[parts[1]] = parts[0]
        if not changed:
            return
        tracked = run_git(
            ["ls-files", "--cached", "--", "*.py"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
        )
        if tracked is None:
            return
        unstaged = run_git(
            ["diff", "--no-renames", "--name-only", "--", "*.py"],
            cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
        )
        if unstaged is None:
            return
        dirty = set(unstaged.decode("utf-8", "replace").split())
        new_files: dict[str, str] = {}
        for path in tracked.decode("utf-8", "replace").split():
            status = changed.get(path)
            if status == "D":
                continue  # gone from the new graph
            if status in ("A", "M"):
                blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
                if blob is None:
                    return
                new_files[path] = blob.decode("utf-8", "replace")
            elif path in dirty:
                # An UNSTAGED worktree edit is not what `git commit` records — reading it could
                # fabricate a cycle that won't exist in the committed tree (review HIGH-1,
                # reproduced). Read the INDEX version for dirty files; clean files read from
                # the worktree (identical content, no git call).
                blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
                if blob is None:
                    return
                new_files[path] = blob.decode("utf-8", "replace")
            else:
                try:
                    new_files[path] = (repo / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue  # unreadable unchanged file: absent from both graphs, no skew
        old_files = dict(new_files)
        for path, status in changed.items():
            if status == "A":
                old_files.pop(path, None)
                continue
            head = run_git(["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
            if head is None:
                old_files.pop(path, None)  # unborn HEAD / new repo: old side simply lacks it
            else:
                old_files[path] = head.decode("utf-8", "replace")
        cycles = find_new_cycles(build_import_graph(old_files), build_import_graph(new_files))
        if not cycles:
            return
        print(
            f"CYCLE ADVISORY: this commit introduces {len(cycles)} new import cycle(s).",
            file=sys.stderr,
        )
        for cycle in cycles:
            path_str = " -> ".join([*cycle, cycle[0]])
            print(f"  EVIDENCE (EXTRACTED): {path_str}", file=sys.stderr)
        print(
            "ACTION: break the cycle — invert the new dependency, or extract the shared piece "
            "into its own module. Run find_cycles for the repo-wide picture.",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — an advisory must never break the commit gate.
        return


def _print_test_integrity_advisory(repo: Path) -> None:
    """The reward-hacking counter (STRATEGY.md Phase 2): when a commit touches BOTH source and
    test files, report each staged test file whose verification power DROPPED — assertions
    removed, skip/xfail markers added, test functions or whole files deleted.

    The both-source-and-tests condition is the discriminating signal ("the fix passes because
    the test got weaker"); a pure test refactor stays silent. Advisory-only: never raises into
    the gate, never affects the exit code. Python-precise v1; other languages pending.
    """
    try:
        listing = run_git(
            ["diff", "--cached", "--no-renames", "--name-status"],
            cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
        )
        if listing is None:
            return
        changed: list[tuple[str, str]] = []
        for line in listing.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                changed.append((parts[0], parts[1]))
        src_touched = any(
            not _is_test_path(path) and path.endswith((".py", ".ts", ".tsx", ".js", ".jsx"))
            for _status, path in changed
        )
        if not src_touched:
            return
        findings: list[str] = []
        total_old = 0
        total_new = 0
        for status, path in changed:
            if not _is_test_path(path) or not path.endswith(".py"):
                continue
            if status == "D":
                old_blob = run_git(
                    ["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
                )
                lost = (
                    _test_metrics(old_blob.decode("utf-8", "replace"))[0]
                    if old_blob is not None
                    else 0
                )
                findings.append(f"{path}: deleted ({lost} assertions lost)")
                continue
            if status != "M":
                continue
            old_blob = run_git(
                ["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
            )
            new_blob = run_git(["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S)
            if old_blob is None or new_blob is None:
                continue
            old_a, old_m, old_t, old_p = _test_metrics(old_blob.decode("utf-8", "replace"))
            new_a, new_m, new_t, new_p = _test_metrics(new_blob.decode("utf-8", "replace"))
            total_old += old_a
            total_new += new_a
            parts_out: list[str] = []
            if new_a < old_a:
                drop = f"assertions {old_a} -> {new_a}"
                if new_p > old_p:
                    # A drop alongside NEW parametrize markers is often consolidation, not
                    # weakening — say so instead of crying wolf (review MED-1).
                    drop += " (parametrize added — may be consolidation, verify)"
                parts_out.append(drop)
            if new_m > old_m:
                parts_out.append(f"skip/xfail markers {old_m} -> {new_m}")
            if new_t < old_t:
                parts_out.append(f"test functions {old_t} -> {new_t}")
            if parts_out:
                findings.append(f"{path}: " + "; ".join(parts_out))
        if not findings:
            return
        print(
            "TEST-INTEGRITY ADVISORY: this commit weakens tests while changing source.",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  EVIDENCE (EXTRACTED): {finding}", file=sys.stderr)
        if total_new >= total_old > 0:
            # Assertions moved BETWEEN test files in the same commit: per-file evidence above is
            # true, but net verification power did not drop — say so (review MED-2).
            print(
                f"  note: net assertions across modified test files {total_old} -> {total_new} "
                "(no net loss — a per-file drop above may be a move)",
                file=sys.stderr,
            )
        print(
            "ACTION: if the source change is a fix, weakening its tests is the reward-hacking "
            "pattern — restore the assertions, or state why each removal is correct. Never "
            "make a failing test pass by deleting or skipping it.",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — an advisory must never break the commit gate.
        return


@functools.lru_cache(maxsize=4)
def _snapshot_tools(repo: Path):  # type: ignore[no-untyped-def]
    """``(CartogateTools, LoadedGraph)`` over the persisted RESOLVED snapshot, or ``None``.

    HONESTY (review of #125): the snapshot is only as fresh as the daemon's last persist — it
    can lag the repo. Every piece of evidence read from it is therefore gated by
    :func:`_fresh_unit` (persist-time content hash == current content), so a stale unit can
    never be cited; and a snapshot whose ``repo_id`` doesn't match this directory (renamed
    clone, CI checkout of a shared snapshot) is rejected outright — its unit strings can't be
    compared against this repo's staged paths. Loading is in-process (gzip JSON): no daemon
    round-trip, no resolver run. Without a usable snapshot the callers stay SILENT.
    """
    from cartogate.mcp.tools import CartogateTools
    from cartogate.store.persist import graph_path, load_graph

    loaded = load_graph(graph_path(repo))
    if loaded is None or loaded.repo_id != repo.name:
        return None
    return (CartogateTools(loaded.store), loaded)


def _fresh_unit(loaded: object, repo: Path, unit: str) -> bool:
    """True iff ``unit``'s CURRENT content matches its persist-time hash — evidence from a unit
    that changed since the snapshot was written is stale and must never be cited."""
    from cartogate.store.persist import content_hash_of

    persist_hash = getattr(loaded, "content_hashes", {}).get(unit)
    if persist_hash is None:
        return False
    return bool(content_hash_of(repo.parent, unit) == persist_hash)


def _staged_units(repo: Path) -> set[str] | None:
    """Store units (``<repo.name>/<path>``) for every staged path, or ``None`` on git failure."""
    listing = run_git(
        ["diff", "--cached", "--no-renames", "--name-only"],
        cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
    )
    if listing is None:
        return None
    return {f"{repo.name}/{path}" for path in listing.decode("utf-8", "replace").split()}


def _unit_tail(unit: str) -> str:
    return unit.split("/", 1)[1] if "/" in unit else unit


def _followthrough_line(  # type: ignore[no-untyped-def]
    tools, loaded, repo: Path, name: str, touched_units: set[str]
) -> str | None:
    """The claims-vs-facts facts for one changed contract: callers / covering tests /
    referencing docs this commit did NOT touch. ``None`` when everything is followed through.

    Every cited unit passes the freshness guard — a unit that changed since the snapshot was
    persisted may no longer contain the reference, so it is never cited (review of #125:
    a stale snapshot manufactured accusations about callers that no longer called).
    """

    def _citable(unit: str | None) -> bool:
        return (
            isinstance(unit, str)
            and unit not in touched_units
            and _fresh_unit(loaded, repo, unit)
        )

    segments: list[str] = []
    refs = tools.find_references(name)
    if refs.get("found"):
        outside = [r for r in refs.get("references", []) if _citable(r.get("unit"))]
        if outside:
            units = sorted({_unit_tail(r["unit"]) for r in outside})
            shown = ", ".join(units[:3])
            segments.append(f"{len(outside)} caller(s) not in this commit ({shown})")
    tests = tools.suggest_tests(symbols=[name])
    untouched_tests = [x for x in tests.get("tests", []) if _citable(x.get("unit"))]
    if untouched_tests:
        segments.append(f"{len(untouched_tests)} covering test(s) untouched")
    docs = tools.doc_drift(symbols=[name])
    untouched_docs = [d for d in docs.get("docs", []) if _citable(d.get("unit"))]
    if untouched_docs:
        segments.append(f"{len(untouched_docs)} referencing doc(s) untouched")
    return "; ".join(segments) if segments else None


def _print_deletion_advisory(repo: Path) -> None:
    """Scope report, the anchor-free half (STRATEGY.md Phase 2): symbols this commit DELETES
    while live references remain — the code-level destructive-edit counter.

    Reference evidence comes from the resolved snapshot; references inside files this commit
    also touches are excluded (they may have been updated in the same change — conservative,
    fewer accusations). Without a snapshot: silent. Advisory-only: never raises into the gate,
    never affects the exit code.
    """
    try:
        from cartogate.extract.languages import language_of, named_signatures_in

        listing = run_git(
            ["diff", "--cached", "--no-renames", "--name-status"],
            cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S,
        )
        if listing is None:
            return
        snapshot = _snapshot_tools(repo)
        if snapshot is None:
            return
        tools, loaded = snapshot
        touched_units = _staged_units(repo)
        if touched_units is None:
            return
        findings: list[str] = []
        for line in listing.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or parts[0] not in ("M", "D"):
                continue
            status, path = parts
            language = language_of(path)
            if language is None:
                continue
            old_blob = run_git(
                ["show", f"HEAD:{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
            )
            if old_blob is None:
                continue
            old_names = {
                q.removeprefix("<snippet>.")
                for q, _sig in named_signatures_in(old_blob.decode("utf-8", "replace"), language)
            }
            if status == "D":
                new_names: set[str] = set()
            else:
                new_blob = run_git(
                    ["show", f":{path}"], cwd=repo, timeout=_ADVISORY_GIT_TIMEOUT_S
                )
                if new_blob is None:
                    continue
                new_names = {
                    q.removeprefix("<snippet>.")
                    for q, _sig in named_signatures_in(
                        new_blob.decode("utf-8", "replace"), language
                    )
                }
            for name in sorted(old_names - new_names):
                # Query with the module-qualified suffix (auth.py + "login" -> "auth.login"):
                # a bare name could uniquely match a DIFFERENT same-named symbol when the
                # snapshot already refreshed past the deletion. Python-only qualification;
                # other languages keep the bare suffix.
                if path.endswith(".py"):
                    from cartogate.importgraph import module_name_for

                    query = f"{module_name_for(path)}.{name}"
                else:
                    query = name
                refs = tools.find_references(query)
                if not refs.get("found"):
                    continue  # ambiguous or unknown in the snapshot: never guess
                live = [
                    r for r in refs.get("references", [])
                    if isinstance(r.get("unit"), str)
                    and r["unit"] not in touched_units
                    and _fresh_unit(loaded, repo, r["unit"])
                ]
                if live:
                    units = sorted({_unit_tail(r["unit"]) for r in live})
                    shown = ", ".join(units[:3])
                    findings.append(
                        f"{name} ({path}) — {len(live)} live reference(s): {shown}"
                    )
        if not findings:
            return
        print(
            f"DELETION ADVISORY: this commit deletes {len(findings)} symbol(s) that still "
            "have references.",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  EVIDENCE (EXTRACTED): {finding}", file=sys.stderr)
        print(
            "ACTION: update or remove the referencing code in this same change, or keep the "
            'symbol — run find_references("<name>") for the full list. Never delete a still-'
            "referenced symbol to silence an error.",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — an advisory must never break the commit gate.
        return


def _print_reference_advisory(repo: Path) -> None:
    """The reference-integrity advisory (STRATEGY.md Phase 1): a staged change that alters an
    established signature is the top documented way agents break callers — say so, with the
    extracted old -> new evidence and the one sanctioned action.

    Advisory-first (the gate-fatigue law): informs, NEVER blocks — and never raises into the
    gate. git prints hook output unconditionally, so the agent sees this even on a passing
    commit. Promotion to a blocking check requires measured ~0 false positives.
    """
    try:
        from cartogate.importgraph import module_name_for

        changes = _staged_contract_changes(repo)
        if not changes:
            return
        print(
            f"ADVISORY: this commit changes {len(changes)} established signature(s) — "
            "callers may now be broken.", file=sys.stderr,
        )
        snapshot = _snapshot_tools(repo)
        touched_units = _staged_units(repo) if snapshot is not None else None
        for path, name, old_sig, new_sig in changes:
            # Signatures are raw source slices — fold whitespace so a multi-line parameter list
            # renders as one evidence line (field transcript 2026-07-05; same treatment as
            # BlockResult.agent_message).
            old_fold = " ".join(old_sig.split())
            new_fold = " ".join(new_sig.split())
            print(
                f"  EVIDENCE (EXTRACTED): {name} ({path}): `{old_fold}` -> `{new_fold}`",
                file=sys.stderr,
            )
            if snapshot is not None and touched_units is not None:
                # Claims-vs-facts, structurally (Phase 2): what this contract change did NOT
                # follow through on — module-qualified lookup so a same-named symbol elsewhere
                # can never answer for this one.
                tools, loaded = snapshot
                query = f"{module_name_for(path)}.{name}" if path.endswith(".py") else name
                line = _followthrough_line(tools, loaded, repo, query, touched_units)
                if line:
                    print(f"  follow-through: {line}", file=sys.stderr)
        first = changes[0][1]
        print(
            "ACTION: update every caller of the changed symbol(s) in this same change, or keep "
            f'the existing signature — run find_references("{first}") to list the callers.',
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — an advisory must never break the commit gate.
        return


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo = Path(args[0]).resolve() if args else Path.cwd()
    try:
        store = InMemoryStore()
        # The duplicate check needs only the signature table — skip name resolution.
        result = index_package(repo, repo_id=repo.name, store=store, resolve=False)
        duplicates = find_duplicate_signatures(list(result.nodes))
    except Exception as exc:  # noqa: BLE001 — a commit gate fails CLOSED on any error.
        print(f"Cartogate pre-commit: gate could not run ({exc}); refusing.", file=sys.stderr)
        return 1
    _print_reference_advisory(repo)  # advisory-only: never raises, never affects the exit
    _print_test_integrity_advisory(repo)  # ditto — the reward-hacking counter
    _print_cycle_advisory(repo)  # ditto — architecture erosion, diff-aware
    _print_deletion_advisory(repo)  # ditto — deletions that still have references
    if duplicates:
        # Judge THE CHANGE, not the history: only duplicates the staged diff introduces block;
        # pre-existing ones are debt to surface, not a reason to refuse an unrelated commit.
        try:
            new_symbols = _staged_new_symbols(repo)
        except Exception:  # noqa: BLE001 — fail closed: treat everything as introduced.
            new_symbols = None
        if new_symbols is not None:
            duplicates, preexisting = _split_introduced(duplicates, new_symbols)
            if preexisting:
                print(
                    f"note: {preexisting} pre-existing duplicate group(s) predate this commit — "
                    "not blocking it. Review with `cartogate stats` / find_duplicate_bodies.",
                    file=sys.stderr,
                )
    if not duplicates:
        from cartogate.stats import record_gate_pass

        record_gate_pass(repo)  # bypass observability: stamp the tree this gate verified
        return 0

    from cartogate.stats import record_block

    print("BLOCKED: this commit introduces duplicate symbols.", file=sys.stderr)
    for (language, signature), members in sorted(duplicates.items(), key=lambda kv: str(kv[0])):
        names = ", ".join(sorted(m.qualified_name for m in members))
        locations = ", ".join(
            sorted(f"{m.location.path}:{m.location.start_line}" for m in members)
        )
        print(
            f"  EVIDENCE (EXTRACTED): [{language.value}] {signature}  ->  {names} ({locations})",
            file=sys.stderr,
        )
        # Record the prevented duplicate so `cartogate stats` can show the value over time.
        record_block(
            repo, kind="commit", signature=signature, language=language.value, existing=names
        )
    print(
        "ACTION: keep ONE definition and update the other call sites to reuse it.\n"
        "Do NOT retry the identical commit, do NOT rename a duplicate to evade the gate, and "
        "never use --no-verify — fix the duplication.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
