"""``cartogate task`` — declare / attest / status / close the active verification contract.

Declaration LINTS the checks and refuses weak ones (spec §5 — scrimp field evidence: the agent
declares its own checks, and self-declared checks are routinely weak). Attestation pins a named
human sign-off to the exact staged tree. The ledger is the audit record for all of it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from cartogate.audit import ledger
from cartogate.contract import checklint, state
from cartogate.contract import verify as cverify
from cartogate.contract.schema import (
    Contract,
    ContractError,
    contract_hash,
    loads,
    parse,
    parse_check_list,
)
from cartogate.surfaces import find_repo_root


def _repo() -> Path:
    return (find_repo_root(Path.cwd()) or Path.cwd()).resolve()


def _load_active(repo: Path) -> Contract | None:
    try:
        contract = state.load(repo)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if contract is None:
        print("no active contract — declare one with `cartogate task declare <file>`",
              file=sys.stderr)
    return contract


def _expand_scope_from_symbols(
    tools: Any, contract: Contract, scope_from: list[str]
) -> Contract:
    """Expand ``--scope-from-symbol`` names into ``scope.files`` via the graph (declare-time).

    Adds each symbol's OWN file (``find_symbol`` — its ``location`` is a DICT
    ``{path, start_line, end_line}``, review M1) plus its ``blast_radius`` callers'
    units. Unit/path prefixes (``<repo>/``) are stripped to repo-relative paths. A symbol the
    snapshot cannot resolve REFUSES the declaration (review M2) — refuse, don't guess; a
    stale snapshot is fixed with ``cartogate index``.
    """
    extra: list[str] = []
    for symbol in scope_from:
        radius = tools.blast_radius(symbol)
        units = {n.get("unit") for n in radius.get("affected", []) if isinstance(n, dict)}
        own = tools.find_symbol(symbol)
        loc = own.get("location") if own.get("found") else None
        own_path = loc.get("path") if isinstance(loc, dict) else None
        if not own.get("found") and not units:
            raise ContractError(
                f"--scope-from-symbol: {symbol!r} not found in the snapshot — fix the name, "
                "or refresh the snapshot (`cartogate index`)"
            )
        if isinstance(own_path, str) and own_path:
            extra.append(own_path.split("/", 1)[1] if "/" in own_path else own_path)
        extra.extend(
            u.split("/", 1)[1] for u in sorted(u for u in units if isinstance(u, str))
            if "/" in u
        )
    if not extra:
        return contract
    raw = dict(contract.raw)
    scope = dict(raw.get("scope", {}))
    scope["files"] = sorted(set(list(scope.get("files", [])) + extra))
    raw["scope"] = scope
    return parse(raw)  # re-validate the expanded contract


def _token_matches(stored: str, token: str | None) -> bool:
    """``blake2b(token) == stored``. Non-constant-time is acceptable: the digest is not a
    secret (it is persisted in state and ledger evidence); the token itself only ever passes
    through the one-way hash, never a comparison."""
    if not token:
        return False
    return hashlib.blake2b(token.encode("utf-8")).hexdigest() == stored


def _declare(
    repo: Path, source: str, scope_from: list[str], lock: bool = False,
    lock_token: str | None = None, unlock: bool = False,
) -> int:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # UnicodeDecodeError is a ValueError (M2)
        print(f"error: cannot read {source}: {exc}", file=sys.stderr)
        return 1
    try:
        contract = loads(text)
    except ContractError as exc:
        print(f"contract refused: {exc}", file=sys.stderr)
        return 1
    if scope_from:
        try:
            from cartogate.mcp.tools import CartogateTools
            from cartogate.store.persist import graph_path, load_graph

            loaded = load_graph(graph_path(repo))
            if loaded is None or loaded.repo_id != repo.name:
                raise OSError("no usable snapshot")
            tools = CartogateTools(loaded.store)
        except Exception:  # noqa: BLE001 — expansion needs the graph; be explicit, not silent
            print(
                "error: --scope-from-symbol needs a resolved snapshot — run "
                "`cartogate daemon start --resolve` (or `cartogate index`) first",
                file=sys.stderr,
            )
            return 1
        try:
            contract = _expand_scope_from_symbols(tools, contract, scope_from)
        except ContractError as exc:
            print(f"contract refused: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 — loadable-but-broken snapshot: clean message
            print(f"error: scope expansion failed ({exc})", file=sys.stderr)
            return 1
    errors, warnings = checklint.lint(contract)
    if errors:
        print("contract refused — weak checks (fix the contract, not the gate):",
              file=sys.stderr)
        for finding in errors:
            print(f"  {finding}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}")
    existing = cverify.active_lock(repo)  # LEDGER-first: task.json edits can't unlock (Critical)
    if existing is not None and not _token_matches(existing, lock_token):
        ledger.append(repo, entry_type="lock_violation", tree=None,
                      evidence={"action": "declare", "had_token": bool(lock_token)})
        print(
            "declare refused: the active contract is LOCKED — supply --lock-token, or "
            "surrender it with `cartogate task close --abandon` (ledgered).",
            file=sys.stderr,
        )
        return 1
    if lock:  # new lock, or an authorized rotation
        token = secrets.token_hex(32)
        new_hash: str | None = hashlib.blake2b(token.encode("utf-8")).hexdigest()
        print(f"lock token (shown ONCE — the driver holds it, never a worker): {token}")
    elif unlock:
        new_hash = None  # explicit, token-authorized removal only
    else:
        new_hash = existing  # an authorized amend CARRIES the lock (review High) — never
        # silently strips it; dropping requires --unlock, rotating requires --lock
    state.save(repo, contract, lock_hash=new_hash)
    h = contract_hash(contract.raw)
    evidence: dict[str, Any] = {"contract": contract.raw, "contract_hash": h,
                                "lint_warnings": warnings, "locked": bool(new_hash),
                                "lock_hash": new_hash}
    if existing is not None:
        # Superseding a locked declaration DISCLOSES the prior token as publicly-verifiable
        # proof of authorization — the ledger walk ignores supersedes without it (6b-variant).
        evidence["prior_token"] = lock_token
    ledger.append(repo, entry_type="contract_declared", tree=None, evidence=evidence)
    print(f"contract declared: {contract.task!r} — {len(contract.checks)} check(s), "
          f"{len(contract.attest)} attestation(s)  [{h[:12]}]")
    return 0


def _attest(repo: Path, name: str, artifacts: list[str]) -> int:
    contract = _load_active(repo)
    if contract is None:
        return 1
    if name not in contract.attest:
        declared = ", ".join(contract.attest) or "(none)"
        print(f"error: attestation {name!r} not declared by the contract (declared: {declared})",
              file=sys.stderr)
        return 1
    tree = cverify.current_tree(repo)
    if tree is None:
        print("error: git cannot produce a tree to pin the attestation to", file=sys.stderr)
        return 1
    hashed: dict[str, str] = {}
    for art in artifacts:
        try:
            hashed[art] = hashlib.blake2b(Path(art).read_bytes()).hexdigest()
        except OSError as exc:
            print(f"error: cannot hash artifact {art}: {exc}", file=sys.stderr)
            return 1
    ledger.append(repo, entry_type="attestation", tree=tree,
                  evidence={"name": name, "contract_hash": contract_hash(contract.raw),
                            "artifacts": hashed})
    print(f"attested {name!r} on tree {tree[:12]} "
          f"({len(hashed)} artifact(s); identity asserted, not authenticated)")
    return 0


def _status(repo: Path, as_json: bool = False) -> int:
    if as_json:
        # The machine probe must be machine-readable in EVERY state (review M3) — a driver
        # parsing stdout gets a JSON object even when nothing is declared or state is corrupt.
        try:
            contract = state.load(repo)
        except ContractError as exc:
            print(json.dumps({"task": None, "ok": False, "error": f"corrupt: {exc}"}))
            return 1
        if contract is None:
            print(json.dumps({"task": None, "ok": False, "error": "no active contract"}))
            return 1
        status = cverify.evaluate(contract, repo)
        divergence = cverify.state_divergence(repo, contract)
        sealed_field = None
        if contract.sealed_hash is not None:
            sealed_field = {"hash": contract.sealed_hash, "count": contract.sealed_count}
        payload = {
            "task": contract.task,
            "contract_hash": contract_hash(contract.raw),
            "locked": cverify.active_lock(repo) is not None,
            "sealed": sealed_field,
            "checks": [
                {"run": result.run, "exit": result.exit_code}
                for result in status.checks
            ],
            "attest": status.attest,
            "tree": status.tree,
            "diverged": status.diverged,
            "state_divergence": divergence,
            # ok mirrors the exit code exactly — a diverged state can never read satisfied,
            # whether a driver gates on the exit code or parses the payload (re-verify info).
            "ok": status.ok and divergence is None,
        }
        print(json.dumps(payload))
        # A diverged state can never read as satisfied — drivers gate on the exit code.
        return 0 if status.ok and divergence is None else 1
    contract = _load_active(repo)
    if contract is None:
        return 1
    status = cverify.evaluate(contract, repo)
    print(f"contract: {contract.task!r}  [{contract_hash(contract.raw)[:12]}]")
    for result in status.checks:
        verdict = "PASS" if result.exit_code == 0 else "FAIL"
        print(f"  [{verdict}] {result.run}")
        if result.exit_code != 0 and result.output.strip():
            tail = result.output.strip().splitlines()[-5:]
            for line in tail:
                print(f"      {line}")
    for name, ok in status.attest.items():
        mark = "ATTESTED" if ok else "PENDING "
        print(f"  [{mark}] {name}" + ("" if ok else
              f"  — run `cartogate task attest {name}` after staging"))
    if status.diverged:
        print("  note: working directory diverges from the index — checks read the worktree")
    return 0 if status.ok else 1


def _seal(repo: Path, file_path: str) -> int:
    """Lint checks in a JSON file and print the blake2b hash and count."""
    try:
        path = Path(file_path)
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read {file_path}: {exc}", file=sys.stderr)
        return 1
    try:
        checks_data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"error: {file_path} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    # ONE validator everywhere (review PR B root cause): seal-time strictness must equal
    # verify-time strictness, or a divergent item silently drops a held-out check.
    try:
        sealed_checks = parse_check_list(checks_data, where="sealed")
    except ContractError as exc:
        print(f"seal refused: {exc}", file=sys.stderr)
        return 1
    if not sealed_checks:
        print("seal refused: sealed checks must be a non-empty list", file=sys.stderr)
        return 1
    lint_errors = [
        f"sealed[{i}] ({c.run!r}): {finding}"
        for i, c in enumerate(sealed_checks)
        for finding in checklint.lint_check(c.run)
    ]
    if lint_errors:
        print("seal refused — weak checks (fix the file, not the gate):", file=sys.stderr)
        for finding in lint_errors:
            print(f"  {finding}", file=sys.stderr)
        return 1
    # Compute hash
    h = hashlib.blake2b(raw_bytes).hexdigest()
    sealed_obj = {"hash": h, "count": len(sealed_checks)}
    print(f"sealed: {json.dumps(sealed_obj)}")
    # Warn if in-repo
    try:
        path.resolve().relative_to(repo.resolve())
        msg = "(⚠ file is INSIDE the repo — custody warning — move it outside)"
        print(f"  {msg}", file=sys.stdout)
    except ValueError:
        pass  # not in repo, no warning
    return 0


def _verify_sealed(repo: Path, file_path: str) -> int:
    """Verify a sealed checks file matches contract hash/count and run the checks."""
    contract = _load_active(repo)
    if contract is None:
        return 2  # usage-level per the driver exit contract: nothing to verify against
    if contract.sealed_hash is None:
        msg = "active contract has no sealed block — verify-sealed is a no-op"
        print(f"error: {msg}", file=sys.stderr)
        return 2
    # Anchor the worker-writable task.json to its ledger declaration (review Critical #1,
    # PR B): a hand-edited sealed block must never mint a sealed_pass — same discipline as
    # the commit gate and status --json.
    divergence = cverify.state_divergence(repo, contract)
    if divergence is not None:
        ledger.append(repo, entry_type="state_divergence", tree=None,
                      evidence={"reason": divergence, "via": "verify-sealed"})
        print(f"error: {divergence} — refusing to verify against unanchored state.",
              file=sys.stderr)
        return 2
    try:
        path = Path(file_path)
        raw_bytes = path.read_bytes()
    except OSError as exc:
        # Driver-side usage error (missing/misnamed path) — never a check failure (M4).
        print(f"error: cannot read {file_path}: {exc}", file=sys.stderr)
        return 2
    # Check hash and count
    h = hashlib.blake2b(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8", "replace")
    try:
        checks_data = json.loads(text)
    except json.JSONDecodeError:
        checks_data = []
    count = len(checks_data) if isinstance(checks_data, list) else 0
    if h != contract.sealed_hash or count != contract.sealed_count:
        ledger.append(repo, entry_type="sealed_mismatch", tree=None,
                      evidence={"contract_hash": contract_hash(contract.raw),
                                "expected": contract.sealed_hash, "got": h})
        err_msg = (
            f"sealed file hash/count mismatch: expected "
            f"{contract.sealed_hash}/{contract.sealed_count}, got {h}/{count}"
        )
        print(f"error: {err_msg}", file=sys.stderr)
        return 1
    # STRICT reconstruction via the one shared validator (review Critical #2): a malformed
    # item must refuse the whole file — silently dropping a held-out check would let the
    # remaining checks mint a false sealed_pass.
    try:
        check_tuple = parse_check_list(checks_data, where="sealed")
    except ContractError as exc:
        ledger.append(repo, entry_type="sealed_mismatch", tree=None,
                      evidence={"contract_hash": contract_hash(contract.raw),
                                "reason": f"malformed sealed file: {exc}"})
        print(f"error: malformed sealed file: {exc}", file=sys.stderr)
        return 1
    results = cverify.run_check_list(check_tuple, repo)
    # Ledger the results BEFORE printing
    all_ok = all(r.exit_code == 0 for r in results)
    tree = cverify.current_tree(repo)
    entry_type = "sealed_pass" if all_ok else "sealed_fail"
    evidence: dict[str, Any] = {
        "contract_hash": contract_hash(contract.raw),
        "checks": [
            {
                "run": r.run,
                "exit": r.exit_code,
                "output_hash": hashlib.blake2b(
                    r.output.encode("utf-8", "replace")
                ).hexdigest(),
            }
            for r in results
        ],
    }
    ledger.append(repo, entry_type=entry_type, tree=tree, evidence=evidence)
    # Print failures if any
    if not all_ok:
        for result in results:
            if result.exit_code != 0 and result.output.strip():
                tail = result.output.strip().splitlines()[-5:]
                for line in tail:
                    # Sanitize the output (review Medium, PR B)
                    safe_line = line.encode("utf-8", "replace").decode("utf-8")
                    print(f"  {safe_line}", file=sys.stderr)
    return 0 if all_ok else 1


def _close(repo: Path, abandon: bool, lock_token: str | None = None) -> int:
    try:
        contract = state.load(repo)
    except ContractError as exc:
        # The gate's corrupt-contract block message names `close --abandon` as a remedy — it
        # must WORK on a contract that can't be parsed (review High, PR B). Ledger the raw
        # bytes' hash in place of the contract hash: attributable, never silent.
        if not abandon:
            print(f"error: {exc} — `cartogate task close --abandon` still retires it",
                  file=sys.stderr)
            return 1
        try:
            raw = state.task_path(repo).read_bytes()
        except OSError:
            raw = b""
        ledger.append(repo, entry_type="contract_closed", tree=None,
                      evidence={"contract_hash": None,
                                "raw_hash": hashlib.blake2b(raw).hexdigest(),
                                "disposition": "abandoned (corrupt)"})
        state.clear(repo)
        print("contract closed (abandoned (corrupt))")
        return 0
    if contract is None:
        print("no active contract — declare one with `cartogate task declare <file>`",
              file=sys.stderr)
        return 1
    lockh = cverify.active_lock(repo)  # LEDGER-first (review Critical)
    if lockh is not None and not abandon and not _token_matches(lockh, lock_token):
        ledger.append(repo, entry_type="lock_violation", tree=None,
                      evidence={"action": "close", "had_token": bool(lock_token)})
        print("close refused: LOCKED contract — supply --lock-token (or --abandon).",
              file=sys.stderr)
        return 1
    if abandon:
        disposition = "abandoned (locked)" if lockh is not None else "abandoned"
    else:
        disposition = "done"
    close_evidence: dict[str, Any] = {"contract_hash": contract_hash(contract.raw),
                                      "disposition": disposition}
    if lockh is not None and not abandon:
        # A 'done' close of a locked contract DISCLOSES the one-time token as proof — the
        # ledger walk ignores unproven 'done' closes (re-verification 6b). Disclosure after
        # retirement is harmless; a new declaration mints a fresh token.
        close_evidence["lock_token"] = lock_token
    ledger.append(repo, entry_type="contract_closed", tree=None, evidence=close_evidence)
    state.clear(repo)
    print(f"contract closed ({disposition})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cartogate task")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_declare = sub.add_parser("declare", help="lint + activate a contract (JSON file or -)")
    p_declare.add_argument("source")
    p_declare.add_argument("--scope-from-symbol", action="append", default=[])
    lock_group = p_declare.add_mutually_exclusive_group()
    lock_group.add_argument("--lock", action="store_true")
    lock_group.add_argument("--unlock", action="store_true")
    p_declare.add_argument("--lock-token")
    p_attest = sub.add_parser("attest", help="record a human sign-off pinned to the staged tree")
    p_attest.add_argument("name")
    p_attest.add_argument("--artifact", action="append", default=[])
    p_status = sub.add_parser("status", help="evaluate the active contract (exit 0 iff satisfied)")
    p_status.add_argument("--json", action="store_true", dest="as_json")
    p_seal = sub.add_parser("seal", help="lint and hash a sealed checks file")
    p_seal.add_argument("file")
    p_verify = sub.add_parser(
        "verify-sealed", help="verify a sealed checks file against the contract"
    )
    p_verify.add_argument("file")
    p_close = sub.add_parser("close", help="retire the active contract")
    p_close.add_argument("--abandon", action="store_true")
    p_close.add_argument("--lock-token")
    ns = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    repo = _repo()
    if ns.cmd == "declare":
        return _declare(
            repo, ns.source, list(ns.scope_from_symbol), lock=ns.lock,
            lock_token=ns.lock_token, unlock=ns.unlock,
        )
    if ns.cmd == "attest":
        return _attest(repo, ns.name, list(ns.artifact))
    if ns.cmd == "status":
        return _status(repo, as_json=ns.as_json)
    if ns.cmd == "seal":
        return _seal(repo, ns.file)
    if ns.cmd == "verify-sealed":
        return _verify_sealed(repo, ns.file)
    return _close(repo, ns.abandon, lock_token=ns.lock_token)


if __name__ == "__main__":
    raise SystemExit(main())
