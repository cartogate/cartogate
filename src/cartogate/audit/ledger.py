"""Tamper-evident, append-only audit ledger for gate decisions.

Every gate decision (commit block/pass, write-time block) is one line in
``.cartogate/ledger.jsonl``. Lines form a blake2b hash chain: each entry's ``hash`` covers the
previous entry's ``hash``, so an edit/deletion/reorder of any *non-tail* entry is detectable by
:func:`verify`. The most-recent entry is subject to a documented tail bound (see
``docs/AUDIT.md``). :func:`verify` also attaches a read-only git coverage report — it never
writes to git.

Recording is best-effort and fails open — it must never raise into a gate. Integrity is checked
on demand by :func:`verify`. All hashing is deterministic (canonical JSON + unseeded blake2b),
matching Cartogate's determinism discipline.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cartogate.daemon.discovery import STATE_DIR, ensure_state_dir
from cartogate.gitio import run_git


def _canonical(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _entry_hash(entry: dict[str, Any]) -> str:
    """blake2b hex of the canonical entry EXCLUDING its own ``hash`` field."""
    payload = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.blake2b(_canonical(payload)).hexdigest()


def decision_hash(entry_type: str, tree: str | None, evidence: dict[str, Any]) -> str:
    """Reproducibility token: blake2b over the decision's type, tree, and evidence.

    Re-running the deterministic gate on the same tree re-derives identical evidence and thus an
    identical token — the "re-run it bit-for-bit" property.
    """
    return hashlib.blake2b(
        _canonical({"type": entry_type, "tree": tree, "evidence": evidence})
    ).hexdigest()


def _git_ident(repo: Path) -> str | None:
    """Git committer identity ``Name <email>`` (timestamp trimmed), or ``None``. Read-only."""
    out = run_git(["var", "GIT_COMMITTER_IDENT"], cwd=repo, timeout=5.0)
    if out is None:
        return None
    text = out.decode("utf-8", "replace").strip()
    cut = text.rfind(">")
    return text[: cut + 1] if cut != -1 else (text or None)


def resolve_actor(repo: Path, env: Mapping[str, str]) -> dict[str, str | None]:
    """Best-effort ``{git, os, agent, src}``. Asserted, NOT authenticated. Never raises."""
    try:
        os_user: str | None = getpass.getuser()
    except Exception:  # noqa: BLE001 — getuser raises if the env has no username
        os_user = None
    agent = env.get("CARTOGATE_ACTOR") or None
    return {
        "git": _git_ident(repo),
        "os": os_user,
        "agent": agent,
        "src": "CARTOGATE_ACTOR" if agent else None,
    }


LEDGER_NAME = "ledger.jsonl"


def ledger_path(repo: Path) -> Path:
    """Where the append-only ledger lives (gitignored, alongside daemon state)."""
    return repo / STATE_DIR / LEDGER_NAME


def _read_raw(repo: Path) -> list[str]:
    try:
        return ledger_path(repo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def read(repo: Path) -> list[dict[str, Any]]:
    """Every parseable ledger entry, oldest first (corrupt lines skipped — see :func:`verify`)."""
    entries: list[dict[str, Any]] = []
    for line in _read_raw(repo):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


_LOCK_STALE_S = 30.0  # appends are near-instant; a lock older than this is a crashed writer


@contextlib.contextmanager
def _lock(repo: Path):  # type: ignore[no-untyped-def]
    """Advisory cross-platform lock so concurrent commits can't interleave chain appends.

    Recovers from a stale lock: a writer that crashed between acquiring and releasing would
    otherwise leave the ``.lock`` file forever, silently wedging every future append. Any lock
    older than ``_LOCK_STALE_S`` is treated as orphaned and broken (appends complete in ms, so a
    lock that old is never live contention).
    """
    lock = repo / STATE_DIR / (LEDGER_NAME + ".lock")
    fd: int | None = None
    for _ in range(50):  # ~5s at 0.1s
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(str(lock)) > _LOCK_STALE_S:
                    os.unlink(str(lock))
                    continue  # broke a crashed writer's lock — retry immediately
            except OSError:
                pass  # the lock vanished under us (released) — fall through and retry
            time.sleep(0.1)
    if fd is None:
        raise TimeoutError("ledger lock busy")
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(str(lock))


def append(
    repo: Path, *, entry_type: str, tree: str | None,
    evidence: dict[str, Any], env: Mapping[str, str] | None = None,
) -> None:
    """Append one decision entry. Best-effort: any failure is swallowed (never breaks a gate)."""
    try:
        ensure_state_dir(repo)
        environ = env if env is not None else os.environ
        with _lock(repo):
            prior = read(repo)
            last = prior[-1] if prior else None
            # A malformed/forged tail line (hand-appended, no seq/hash) must not silently wedge
            # every FUTURE append — the violation telemetry recorded after a forgery is the
            # point. Recover a usable seq/prev; the chain break AT the forgery stays visible
            # to verify() (tamper-evidence preserved).
            try:
                next_seq = (int(last["seq"]) + 1) if last else 0
            except (KeyError, TypeError, ValueError):
                next_seq = len(prior)
            entry: dict[str, Any] = {
                "v": 1,
                "seq": next_seq,
                "ts": int(time.time()),
                "type": entry_type,
                "actor": resolve_actor(repo, environ),
                "tree": tree,
                "evidence": evidence,
                "decision_hash": decision_hash(entry_type, tree, evidence),
                "prev": str(last.get("hash", "")) if isinstance(last, dict) else "",
            }
            entry["hash"] = _entry_hash(entry)
            with ledger_path(repo).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — recording is best-effort; never raise into a gate.
        return


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :func:`verify`. ``ok`` is INTEGRITY only; ``coverage`` is a separate report."""

    ok: bool
    entries: int
    failure: str | None = None
    failure_seq: int | None = None
    coverage: dict[str, Any] | None = None


_COVERAGE_WINDOW = 20


def _anchor_check(repo: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-check ``commit_pass`` tree hashes against git history (READ-ONLY). REPORT-ONLY.

    This is coverage, never an integrity verdict. Only ``commit_pass`` entries are considered: a
    blocked commit never lands in git (its entry carries no tree), and — critically — the pass
    stamp is written at pre-commit time via ``git write-tree``, i.e. BEFORE the commit object
    exists. So a ``commit_pass`` whose tree is not (yet) in history is a *pending or aborted*
    commit, NOT fabrication — treating it as tampering would false-alarm on every ordinary aborted
    commit. Integrity is the hash chain's job (:func:`verify`); this only surfaces:

    - git -> ledger: a recent commit whose tree has no ``commit_pass`` stamp = a bypass.
    - ``pending``: ``commit_pass`` stamps whose tree isn't in history = aborted/uncommitted, FYI.
    """
    passes = [e for e in entries if e.get("type") == "commit_pass" and e.get("tree")]
    out = run_git(["log", "--no-merges", "--format=%H %T"], cwd=repo, timeout=10.0)
    if out is None:
        return {"git": False}
    commits: list[tuple[str, str]] = []
    trees: set[str] = set()
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
            trees.add(parts[1])
    stamped = {e["tree"] for e in passes}
    window = commits[:_COVERAGE_WINDOW]
    unverified = [sha[:8] for sha, tree in window if tree not in stamped]
    return {
        "git": True,
        "commits": len(window),
        "verified": len(window) - len(unverified),
        "unverified": unverified,
        "pending": sum(1 for e in passes if e["tree"] not in trees),
    }


def verify(repo: Path) -> VerifyResult:
    """Recompute the hash chain; attach the read-only git coverage report.

    ``ok`` reflects INTEGRITY only — False on the first corrupt line, hash mismatch, or chain
    break. The git anchor (:func:`_anchor_check`) is coverage, never a verdict (see its docstring
    for why ledger->git fabrication can't be distinguished from an aborted commit).

    KNOWN BOUND (documented in docs/AUDIT.md): a bare hash chain cannot detect an edit-and-rehash
    or truncation of the *tail* (most-recent) entry, since nothing downstream references it. A
    removed ``commit_pass`` tail entry still resurfaces as an unverified commit in ``coverage``.
    """
    prev = ""
    parsed: list[dict[str, Any]] = []
    for i, line in enumerate(_read_raw(repo)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return VerifyResult(False, i, f"corrupt JSON at line {i}", i)
        if _entry_hash(entry) != entry.get("hash"):
            return VerifyResult(False, i, f"hash mismatch at seq {entry.get('seq')}",
                                entry.get("seq"))
        if entry.get("prev") != prev:
            return VerifyResult(False, i, f"chain break at seq {entry.get('seq')}",
                                entry.get("seq"))
        prev = entry["hash"]
        parsed.append(entry)
    return VerifyResult(True, len(parsed), coverage=_anchor_check(repo, parsed))
