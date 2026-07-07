"""``cartogate stats`` — make Cartogate's value visible.

Cartogate fails open and its tools are advisory, so a developer rarely *sees* the value they're
getting. ``stats`` surfaces it two ways:

1. **What Cartogate knows about the repo right now** — symbols/files/languages it indexes, and the
   code-health candidates it can point at (near-duplicate bodies, dead code, import cycles).
2. **What it has prevented** — a persistent tally of duplicate-introducing commits the gate
   refused, recorded to ``.cartogate/events.jsonl`` by :func:`record_block` (append-only, off the
   hot path — a BLOCK is rare). Every line is one prevented duplicate: a concrete audit trail.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from cartogate.daemon.discovery import STATE_DIR
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import NodeKind
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_repo_root

_EVENTS_NAME = "events.jsonl"


def events_path(repo: Path) -> Path:
    """Where the block-event audit trail lives (gitignored, alongside daemon state)."""
    return repo / STATE_DIR / _EVENTS_NAME


def record_block(repo: Path, *, kind: str, signature: str, language: str, existing: str) -> None:
    """Append one BLOCK event. Best-effort: it must never raise into the gate that called it."""
    try:
        from cartogate.daemon.discovery import ensure_state_dir

        path = events_path(repo)
        ensure_state_dir(repo)  # self-ignoring even when events.jsonl is the first writer
        entry = {
            "ts": int(time.time()),
            "kind": kind,  # "commit" | "write"
            "signature": signature,
            "language": language,
            "existing": existing,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # recording is a nicety; a full disk must not break the gate


def read_blocks(repo: Path) -> list[dict[str, Any]]:
    """Every recorded block event (skipping any corrupt line), oldest first."""
    try:
        lines = events_path(repo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize(repo: Path) -> dict[str, Any]:
    """Index ``repo`` and report what Cartogate knows + what it has prevented."""
    store = InMemoryStore()
    result = index_package(repo, repo_id=repo.name, store=store)
    tools = CartogateTools(store)
    languages = Counter(
        node.language.value for node in result.nodes if node.kind is NodeKind.SYMBOL
    )
    symbols = sum(languages.values())
    blocks = read_blocks(repo)
    return {
        "repo": str(repo),
        "files": result.files_indexed,
        "symbols": symbols,
        "edges": len(result.edges),
        "languages": dict(languages.most_common()),
        "duplicate_bodies": tools.find_duplicate_bodies()["count"],
        "dead_code": tools.find_dead_code()["count"],
        "cycles": tools.find_cycles()["count"],
        "blocks_total": len(blocks),
        "blocks_by_language": dict(Counter(b.get("language", "?") for b in blocks).most_common()),
        "last_block": blocks[-1] if blocks else None,
    }


_GATE_RUNS_NAME = "gate_runs.jsonl"
_COVERAGE_WINDOW = 20  # recent commits examined by gate_coverage
_GIT_TIMEOUT_S = 10.0


def gate_runs_path(repo: Path) -> Path:
    """Where PASSING commit-gate runs stamp the staged tree hash (bypass observability)."""
    return repo / STATE_DIR / _GATE_RUNS_NAME


def record_gate_pass(repo: Path) -> None:
    """Stamp a PASSING gate run with the staged tree hash (``git write-tree``).

    The stamp is the deterministic link between "the gate ran" and "this exact commit": a
    commit whose tree carries no stamp entered without the gate — ``--no-verify``, or a repo
    where the hook wasn't installed. Best-effort: it must never raise into the gate.
    """
    from cartogate.gitio import run_git

    try:
        tree = run_git(["write-tree"], cwd=repo, timeout=_GIT_TIMEOUT_S)
        if tree is None:
            return
        from cartogate.daemon.discovery import ensure_state_dir

        ensure_state_dir(repo)
        entry = {"tree": tree.decode("ascii", "replace").strip(), "ts": int(time.time())}
        with gate_runs_path(repo).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — observability must never break the gate.
        return


def _stamped_trees(repo: Path) -> set[str]:
    try:
        lines = gate_runs_path(repo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    trees: set[str] = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        tree = entry.get("tree")
        if isinstance(tree, str):
            trees.add(tree)
    return trees


def gate_coverage(repo: Path) -> dict[str, Any]:
    """Recent commits split into gate-verified vs unverified — deterministic tree-hash equality.

    ``unverified`` commits entered WITHOUT a passing gate run for their exact tree: a
    ``--no-verify`` bypass, a commit made before the gate stamped, or a repo without the hook.
    """
    from cartogate.gitio import run_git

    # --no-merges: `git merge` never runs pre-commit, so a merge commit can't carry a stamp —
    # counting it as "unverified" would be a false accusation.
    log = run_git(
        ["log", f"-{_COVERAGE_WINDOW}", "--no-merges", "--format=%H %T"],
        cwd=repo, timeout=_GIT_TIMEOUT_S,
    )
    if log is None:
        return {"commits": 0, "verified": 0, "unverified": []}
    stamped = _stamped_trees(repo)
    commits = 0
    verified = 0
    unverified: list[str] = []
    for line in log.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, tree = parts
        commits += 1
        if tree in stamped:
            verified += 1
        else:
            unverified.append(sha[:8])
    return {"commits": commits, "verified": verified, "unverified": unverified}


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (ValueError, TypeError, OSError):
        return "?"


def run(root: Path) -> int:
    repo = (find_repo_root(root.resolve()) or root).resolve()
    s = summarize(repo)
    print(f"Cartogate stats — {repo}\n")
    langs = ", ".join(f"{lang} {n}" for lang, n in s["languages"].items()) or "—"
    print("Graph:")
    print(f"  {s['symbols']} symbols across {s['files']} files, {s['edges']} edges")
    print(f"  languages: {langs}")
    print("\nCode-health candidates (advisory):")
    print(f"  near-duplicate bodies: {s['duplicate_bodies']}")
    print(f"  dead-code candidates:  {s['dead_code']}")
    print(f"  import cycles:         {s['cycles']}")
    print("\nPrevented (commit gate):")
    if s["blocks_total"] == 0:
        print("  no duplicate-introducing commits blocked yet")
    else:
        by_lang = ", ".join(f"{lang} {n}" for lang, n in s["blocks_by_language"].items())
        print(f"  {s['blocks_total']} duplicate-introducing commit(s) refused ({by_lang})")
        last = s["last_block"]
        when = _fmt_ts(last.get("ts"))
        print(f"  most recent: {last['signature']} → {last['existing']} ({when})")
    cov = gate_coverage(repo)
    if cov["commits"]:
        print(f"\ngate coverage (last {cov['commits']} commits):")
        print(f"  {cov['verified']} gate-verified, {len(cov['unverified'])} unverified")
        if cov["unverified"]:
            shown = ", ".join(cov["unverified"][:5])
            print(
                f"  unverified (bypassed with --no-verify, or made without the gate): {shown}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args and not args[0].startswith("-") else Path(".")
    return run(root)


if __name__ == "__main__":
    raise SystemExit(main())
