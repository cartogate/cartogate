"""``cartogate audit`` — verify and inspect the tamper-evident gate-decision ledger."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from cartogate.audit import ledger
from cartogate.surfaces import find_repo_root


def _repo(root: Path) -> Path:
    return (find_repo_root(root.resolve()) or root).resolve()


def _verify(repo: Path) -> int:
    res = ledger.verify(repo)
    if not res.ok:
        print(f"LEDGER TAMPERED: {res.failure} (seq {res.failure_seq})", file=sys.stderr)
        return 1
    print(f"Ledger intact — {res.entries} decision(s) chained.")
    cov = res.coverage or {}
    if cov.get("git") and cov.get("commits"):
        print(f"gate coverage (last {cov['commits']} commits): "
              f"{cov['verified']} verified, {len(cov['unverified'])} unverified")
        if cov["unverified"]:
            print("  unverified (bypassed or pre-ledger): " + ", ".join(cov["unverified"][:5]))
    return 0


def _log(repo: Path) -> int:
    for e in ledger.read(repo):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(e.get("ts", 0))))
        actor = e.get("actor", {})
        who = actor.get("agent") or actor.get("git") or actor.get("os") or "?"
        print(f"[{when}] {e.get('type'):12} by {who}  seq={e.get('seq')}")
    print("\n(actor identities are asserted, not authenticated)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sub = args[0] if args else "verify"
    repo = _repo(Path("."))
    if sub == "verify":
        return _verify(repo)
    if sub == "log":
        return _log(repo)
    print("usage: cartogate audit {verify,log}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
