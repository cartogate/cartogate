"""``cartogate doctor`` — one command that answers "is Cartogate actually working?"

Cartogate fails *open* (a problem never blocks your edit), which is safe but means an outage is
silent: the gate quietly stops protecting you. ``doctor`` makes the invisible visible — it proves
the gate answers end-to-end, reports the warm graph's health, and checks that the enforcement
hooks are wired. It exits non-zero if anything is actually broken (so CI / scripts can gate on it),
while treating "no daemon" and "an optional hook isn't installed" as advisories, not failures.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import time
from pathlib import Path

from cartogate.daemon import client as daemon_client
from cartogate.daemon.discovery import is_pid_alive, log_path, read_discovery
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_repo_root, gate_proposed_source

_OK, _WARN, _FAIL = "[ok]  ", "[warn]", "[fail]"

#: A signature no real codebase defines, so the probe is guaranteed novel (gate must NOT block it).
_PROBE_SOURCE = "def __cartogate_doctor_probe__():\n    return 1\n"
_PROBE_SIGNATURE = "def __cartogate_doctor_probe__():"


class _Report:
    """Collects check lines and whether any hard failure occurred (-> non-zero exit)."""

    def __init__(self) -> None:
        self.failed = False

    def ok(self, msg: str) -> None:
        print(f"{_OK} {msg}")

    def warn(self, msg: str) -> None:
        print(f"{_WARN} {msg}")

    def fail(self, msg: str) -> None:
        self.failed = True
        print(f"{_FAIL} {msg}")


def _check_env(report: _Report) -> None:
    """Report install health: version, the git dependency, and the extraction deps."""
    from cartogate import __version__

    py = sys.version_info
    report.ok(f"cartogate {__version__} (Python {py.major}.{py.minor}.{py.micro})")
    if shutil.which("git") is not None:
        report.ok("git on PATH — gitignore-aware indexing + lazy daemon refresh enabled")
    else:
        report.warn(
            "git not on PATH — indexing falls back to a fixed noise-dir walk (no .gitignore "
            "awareness) and the daemon can't detect changes lazily"
        )
    missing = [m for m in ("tree_sitter_python", "jedi") if importlib.util.find_spec(m) is None]
    if missing:
        report.fail(
            f"extraction deps missing ({', '.join(missing)}) — reinstall Cartogate "
            "(`pipx install 'cartogate'` / `pip install cartogate`)"
        )
    else:
        report.ok("extraction deps present (tree-sitter grammars + jedi)")
    # The MCP SDK is an optional extra; the `cartogate-mcp` server can't start without it. Advisory
    # — the gate and CLI work fine without MCP — but it's the usual cause of a "dead" MCP server.
    if importlib.util.find_spec("mcp") is None:
        report.warn(
            "MCP SDK not installed — `cartogate-mcp` can't start (a common cause of a 'dead' MCP "
            "connection). Add it: `pipx inject cartogate mcp`, or reinstall with the [mcp] extra. "
            "Not needed for the gate/CLI."
        )
    else:
        report.ok("MCP SDK present (cartogate-mcp server can start)")


def _check_daemon(repo: Path, report: _Report) -> bool:
    """Report daemon + warm-graph health. Returns True if a healthy daemon answered."""
    info = read_discovery(repo)
    if info is None or not is_pid_alive(info.pid):
        report.warn(
            "daemon not running — the gate still works in-process (slower). "
            "Start the warm daemon with `cartogate daemon start`."
        )
        return False
    try:
        health = daemon_client.health(repo)
    except daemon_client.DaemonUnavailableError as exc:
        report.fail(f"daemon discovered (pid {info.pid}) but not answering: {exc}")
        return False

    report.ok(f"daemon running (pid {health.get('pid')}) — up {health.get('uptime_s')}s")
    report.ok(
        f"graph: {health.get('nodes')} nodes, {health.get('edges')} edges, "
        f"{health.get('units')} files"
    )
    last = health.get("last_refresh")
    if isinstance(last, dict):
        report.ok(
            f"last refresh: {last.get('mode')} ({last.get('reindexed')} files reparsed); "
            f"{health.get('refreshes')} refresh(es) total"
        )
    if health.get("errors"):
        report.warn(f"daemon recorded {health.get('errors')} refresh error(s): "
                    f"{health.get('last_error')}")
    return True


def _check_gate_probe(repo: Path, used_daemon: bool, report: _Report) -> None:
    """Prove the gate answers end-to-end with a guaranteed-novel probe (must not block)."""
    if used_daemon:
        try:
            verdict = daemon_client.query(
                repo, "check_duplicate", {"signature": _PROBE_SIGNATURE, "language": "python"}
            )
        except daemon_client.DaemonUnavailableError as exc:
            report.fail(f"gate probe failed against the daemon: {exc}")
            return
        if verdict.get("blocked"):
            report.fail("gate probe returned BLOCK for a novel signature (false positive)")
        else:
            report.ok("gate answers (warm daemon): a novel signature is correctly allowed")
        return

    # No daemon: prove the in-process fallback works, and time it (the cost the hook pays).
    try:
        store = InMemoryStore()
        started = time.monotonic()
        index_package(repo, repo_id=repo.name, store=store, resolve=False)
        elapsed = time.monotonic() - started
        blocked = gate_proposed_source(store, _PROBE_SOURCE)
    except Exception as exc:  # noqa: BLE001 — doctor reports any failure rather than crashing
        report.fail(f"in-process gate could not index this repo: {exc}")
        return
    if blocked:
        report.fail("gate probe returned BLOCK for a novel signature (false positive)")
    else:
        report.ok(
            f"gate answers (in-process): indexed {len(store.visible_node_ids())} symbols "
            f"in {elapsed:.2f}s, novel signature allowed"
        )


def _check_hooks(repo: Path, report: _Report) -> None:
    """Check the enforcement hooks are wired (advisory — not every repo wires every surface)."""
    pre_commit = repo / ".git" / "hooks" / "pre-commit"
    hook_text = _read(pre_commit) if pre_commit.exists() else ""
    if "cartogate-precommit" in hook_text:
        report.ok("git pre-commit hook installed (the fail-closed backstop)")
    elif "cartogate" in hook_text:
        # The pre-pinning hook form runs bare `python -m ...` — BROKEN wherever PATH's python
        # lacks cartogate (pipx installs), and a broken backstop trains agents to --no-verify.
        report.warn(
            "git pre-commit hook is the old unpinned form (bare `python -m`) — re-run "
            "`cartogate init --agent <tool>` to upgrade it"
        )
    else:
        report.warn(
            "git pre-commit hook not installed — install it with "
            "`cp hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`"
        )

    claude = repo / ".claude" / "settings.json"
    claude_text = _read(claude) if claude.exists() else ""
    # Both generations count: the installed command (init-wired, F-13) and the old script path.
    if "cartogate-write-gate" in claude_text or "pretooluse_gate" in claude_text:
        report.ok("Claude Code PreToolUse gate wired (write-time block)")
    else:
        report.warn(
            "Claude Code PreToolUse gate not wired — run `cartogate init --agent claude`"
        )

    windsurf = repo / ".windsurf" / "hooks.json"
    windsurf_text = _read(windsurf) if windsurf.exists() else ""
    if "pre_write_code" in windsurf_text and "cartogate" in windsurf_text:
        report.ok("Windsurf pre_write_code gate wired (write-time block)")
    else:
        report.warn(
            "Windsurf pre_write_code gate not wired — run `cartogate init --agent windsurf`"
        )

    from cartogate.stats import gate_coverage

    cov = gate_coverage(repo)
    if cov["unverified"] and pre_commit.exists():
        shown = ", ".join(cov["unverified"][:3])
        report.warn(
            f"{len(cov['unverified'])} of the last {cov['commits']} commits entered without a "
            f"passing gate run ({shown}) — bypassed with --no-verify, or made without the gate"
        )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run(root: Path) -> int:
    repo = (find_repo_root(root.resolve()) or root).resolve()
    report = _Report()
    print(f"Cartogate doctor — {repo}\n")

    _check_env(report)
    used_daemon = _check_daemon(repo, report)
    _check_gate_probe(repo, used_daemon, report)
    log = log_path(repo)
    if log.exists():
        report.ok(f"daemon log: {log}")
    _check_hooks(repo, report)

    from cartogate.stats import read_blocks

    blocked = read_blocks(repo)
    if blocked:
        report.ok(f"prevented {len(blocked)} duplicate-introducing commit(s) — `cartogate stats`")

    print()
    if report.failed:
        print("doctor: Cartogate has a problem (see [fail] above).")
        return 1
    print("doctor: Cartogate is healthy.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args and not args[0].startswith("-") else Path(".")
    return run(root)


if __name__ == "__main__":
    raise SystemExit(main())
