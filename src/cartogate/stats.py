"""``cartogate stats`` — make Cartogate's value visible.

Cartogate fails open and its tools are advisory, so a developer rarely *sees* the value they're
getting. ``stats`` surfaces it two ways:

1. **What Cartogate knows about the repo right now** — symbols/files/languages it indexes, and the
   code-health candidates it can point at (near-duplicate bodies, dead code, import cycles).
2. **What it has prevented** — a persistent tally of duplicate-introducing commits the gate
   refused, recorded to the tamper-evident audit ledger (``.cartogate/ledger.jsonl``) by
   :func:`record_block`. Every block entry is one prevented duplicate; ``cartogate audit verify``
   proves the trail is intact.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import NodeKind
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_repo_root

_UNFALSIFIED_MIN = 5


def record_block(repo: Path, *, kind: str, signature: str, language: str, existing: str) -> None:
    """Record one prevented duplicate to the tamper-evident audit ledger.

    ``kind`` "commit" -> a ``commit_block`` entry, "write" -> a ``write_block`` entry. A blocked
    commit never lands in git, so the entry carries no tree (only ``commit_pass`` entries are
    git-anchored). Best-effort — :func:`cartogate.audit.ledger.append` swallows all errors.
    """
    from cartogate.audit import ledger

    entry_type = "commit_block" if kind == "commit" else "write_block"
    ledger.append(
        repo, entry_type=entry_type, tree=None,
        evidence={"signature": signature, "language": language, "existing": existing},
    )


def read_blocks(repo: Path) -> list[dict[str, Any]]:
    """Every recorded block (commit or write) from the ledger, oldest first, in the legacy shape."""
    from cartogate.audit import ledger

    blocks: list[dict[str, Any]] = []
    for e in ledger.read(repo):
        if e.get("type") not in ("commit_block", "write_block"):
            continue
        ev = e.get("evidence", {})
        blocks.append({
            "ts": e.get("ts"),
            "kind": "commit" if e["type"] == "commit_block" else "write",
            "signature": ev.get("signature"),
            "language": ev.get("language"),
            "existing": ev.get("existing"),
        })
    return blocks


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


_GIT_TIMEOUT_S = 10.0


def record_gate_pass(repo: Path) -> None:
    """Stamp a PASSING commit gate with the staged tree hash, as a ``commit_pass`` ledger entry.

    The tree (``git write-tree``) is the deterministic link between "the gate ran" and "this exact
    commit": :func:`gate_coverage` flags any commit whose tree carries no such stamp (a
    ``--no-verify`` bypass, or a repo without the hook). Best-effort — never raises into the gate.
    """
    from cartogate.audit import ledger
    from cartogate.gitio import run_git

    tree = run_git(["write-tree"], cwd=repo, timeout=_GIT_TIMEOUT_S)
    if tree is None:
        return
    ledger.append(
        repo, entry_type="commit_pass",
        tree=tree.decode("ascii", "replace").strip(), evidence={},
    )


def gate_coverage(repo: Path) -> dict[str, Any]:
    """Recent commits split into gate-verified vs unverified (deterministic tree-hash equality).

    Delegates to the ledger's read-only git anchor: a commit whose tree matches no ``commit_pass``
    stamp entered WITHOUT a passing gate run — a ``--no-verify`` bypass, a commit made before the
    gate stamped, or a repo without the hook. Merge commits are excluded (they never run the hook).
    """
    from cartogate.audit import ledger

    cov = ledger.verify(repo).coverage or {}
    if not cov.get("git"):
        return {"commits": 0, "verified": 0, "unverified": []}
    return {
        "commits": cov.get("commits", 0),
        "verified": cov.get("verified", 0),
        "unverified": cov.get("unverified", []),
    }


def contract_summary(repo: Path) -> dict[str, Any]:
    """Contract observability (spec §8): evidence mix + never-failed ("unfalsified") checks.

    Lint cannot catch a semantically wrong-but-passing check (scrimp defect t2); the honest
    bound is surfacing checks that have NEVER failed across many runs for human review —
    unfalsified, not trustworthy.
    """
    from cartogate.audit import ledger

    declared = checks = attests = 0
    runs: dict[str, tuple[int, int]] = {}  # folded run -> (times run, times failed)
    for entry in ledger.read(repo):
        etype = entry.get("type")
        ev = entry.get("evidence")
        if not isinstance(ev, dict):
            continue  # tampered/hand-edited entries are skipped, never crash the reader (M3)
        if etype == "contract_declared":
            declared += 1
            contract = ev.get("contract")
            if isinstance(contract, dict):
                check_list = contract.get("checks")
                attest_list = contract.get("attest")
                checks += len(check_list) if isinstance(check_list, list) else 0
                attests += len(attest_list) if isinstance(attest_list, list) else 0
        elif etype in ("contract_pass", "contract_fail", "sealed_pass", "sealed_fail"):
            check_entries = ev.get("checks")
            for c in check_entries if isinstance(check_entries, list) else []:
                if not isinstance(c, dict):
                    continue
                key = " ".join(str(c.get("run", "")).split())
                ran, failed = runs.get(key, (0, 0))
                runs[key] = (ran + 1, failed + (0 if c.get("exit") == 0 else 1))
    unfalsified = sorted(
        run for run, (ran, failed) in runs.items() if ran >= _UNFALSIFIED_MIN and failed == 0
    )
    return {"declared": declared, "checks": checks, "attests": attests,
            "unfalsified": unfalsified}


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
    cs = contract_summary(repo)
    if cs["declared"]:
        print(f"\nContracts: {cs['declared']} declared "
              f"(evidence mix: {cs['checks']} check(s) / {cs['attests']} attestation(s))")
        if cs["unfalsified"]:
            print(f"  unfalsified checks (never failed in >= {_UNFALSIFIED_MIN} runs — "
                  "review them): " + "; ".join(cs["unfalsified"][:3]))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args and not args[0].startswith("-") else Path(".")
    return run(root)


if __name__ == "__main__":
    raise SystemExit(main())
