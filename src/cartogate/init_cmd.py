"""``cartogate init`` — set up Cartogate in a repo, in two modes.

**Default (advisory, non-invasive):** register the MCP server config (A, per-agent, merge-safe —
never clobbering an existing server) and start the warm daemon (D). This makes the tools available
without writing anything INTO the repo and without installing a commit gate.

**``--agent <tool>`` (opinionated adoption):** additionally drop the tool's rule nudge (B — the
per-tool rule file + AGENTS.md) and install the hard gates: **commit-time** (C,
``python -m cartogate.precommit``) and, where the harness has a pre-write hook surface,
**write-time** (C2, ``cartogate-write-gate``). Gated behind ``install_gate`` so a bare ``init``
never touches repo files or hooks.

Everything is idempotent and ``--dry-run`` previews without writing. ``--agent claude`` /
``--agent devin`` also wire the WRITE-TIME gate (C2): the installed ``cartogate-write-gate``
command goes into ``.claude/settings.json`` (PreToolUse) / ``.devin/hooks.v1.json``
(top-level PreToolUse) — the only enforcement layer that acts inside the agent's loop (F-13).
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
import tomllib
from pathlib import Path

from cartogate.surfaces import find_repo_root

MCP_COMMAND = "cartogate-mcp"

#: The always-on rule that tells an agent *when* to call the tools (surface B).
RULE_NUDGE = """# Cartogate rules

**Gate (can BLOCK):** before creating a function or class, call `check_duplicate(signature)`
and reuse any existing symbol it returns.

**Advisory (never block):** before changing an exported symbol, call `blast_radius` (or
`impact_summary`). After editing, call `suggest_tests` and `doc_drift`. Navigate with
`find_symbol` / `find_references`. Review health with `find_cycles`,
`find_duplicate_bodies`, `find_dead_code`.

## cartogate vs grep

Reach for cartogate FIRST when the question is structural - it answers with resolved facts and
file:line locations in one call:
- who calls / references X -> `find_references` (call sites included)
- what breaks if X changes -> `blast_radius` / `impact_summary`
- does X already exist -> `check_duplicate` / `find_symbol`
- who implements/subclasses X -> `implementations`
- show me X's code -> `read_symbol`
- orient in this repo -> `repo_map` (first call in an unfamiliar codebase)

grep/find remain right for raw text: comments, strings, logs, config, TODOs, and anything not
indexed. If a grep for an identifier comes back noisy, that is the cue to switch to
`find_references`.

These MCP tools are deterministic and rest only on EXTRACTED structural facts. A BLOCK means a
real duplicate/contract break — fix the underlying issue.

"""

#: Block-recovery rule (STRATEGY.md law 1). Separate constant so older AGENTS.md installs that
#: predate it can be upgraded by appending just this paragraph.
_BLOCK_RECOVERY = (
    "**On a BLOCK:** follow the ACTION line in the block message (usually: reuse the existing\n"
    "symbol it names). Never retry the identical call, never rename the new symbol to evade\n"
    "the gate, and never bypass with `--no-verify`.\n"
)

RULE_NUDGE = RULE_NUDGE + _BLOCK_RECOVERY


def _workspace_section() -> str:
    """The workspace rule: some editors (Windsurf) don't tell the MCP server which project is open,
    so the agent must. Machine-independent on purpose — the agent uses ITS OWN workspace root (no
    hardcoded absolute path, so the rule is committable/shareable and correct on any clone)."""
    return (
        "\n## Workspace (do this first)\n\n"
        "Some editors (e.g. Devin Desktop) don't pass the open project to the MCP server, so "
        "Cartogate "
        "starts without knowing which repository you're in. On your FIRST Cartogate call of a "
        "session, include the `workspace_root` parameter (every Cartogate tool accepts it) set to "
        "the absolute path of THIS repository's root (the workspace folder open in your editor) — "
        "the call configures the workspace and runs in one step. If a tool ever returns "
        '`"action": "set_workspace"`, call `set_workspace` with `root` = that same path, then '
        "retry.\n"
    )


def _rule_text() -> str:
    """The full rule: the tool-usage nudge plus the workspace/``set_workspace`` step."""
    return RULE_NUDGE + _workspace_section()

def _precommit_hook_text() -> str:
    """The pre-commit hook body — interpreter-PINNED (field bug, 2026-07-04).

    A bare ``python -m cartogate.precommit`` breaks on machines where PATH's python lacks
    cartogate (pipx installs) — the fail-closed backstop ERRORS and agents respond by
    bypassing with ``--no-verify``. The hook prefers the ``cartogate-precommit`` console
    script (PATH-independent of which python is first, upgrade-stable), falling back to the
    exact interpreter that ran init. ``.git/hooks`` is per-machine, so the absolute path is
    correct by construction, never committed.
    """
    python = Path(sys.executable).as_posix()
    return (
        "#!/bin/sh\n"
        "# Cartogate duplicate-signature gate — installed by `cartogate init`.\n"
        "if command -v cartogate-precommit >/dev/null 2>&1; then\n"
        "  exec cartogate-precommit\n"
        "fi\n"
        f'exec "{python}" -m cartogate.precommit\n'
    )


class _Report:
    """Accumulates what init did / skipped, and tracks whether anything actually changed."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.changed = False

    def did(self, msg: str) -> None:
        self.changed = True
        print(f"  {'would write' if self.dry_run else 'wrote'}: {msg}")

    def skip(self, msg: str) -> None:
        print(f"  skip: {msg}")

    def note(self, msg: str) -> None:
        print(f"  note: {msg}")


def detect_agents(root: Path) -> set[str]:
    """Which agents this repo already looks set up for (by their marker files/dirs)."""
    found: set[str] = set()
    claude_markers = (".claude", "CLAUDE.md", ".mcp.json")
    if any((root / marker).exists() for marker in claude_markers):
        found.add("claude")
    if (root / ".cursor").exists():
        found.add("cursor")
    if (root / ".devin").exists() or (root / ".windsurf").exists():
        found.add("devin")  # Devin Desktop/CLI; legacy .windsurf/ maps here too
    if (root / ".vscode").exists():
        found.add("vscode")
    if (root / ".codex").exists():
        found.add("codex")
    return found


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, object], report: _Report) -> None:
    if report.dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _ensure_mcp(root: Path, rel: str, key: str, report: _Report) -> None:
    """Add a merge-safe ``cartogate`` MCP entry under ``key`` in ``rel`` (never clobbering)."""
    path = root / rel
    data = _read_json(path)
    servers = data.get(key)
    if not isinstance(servers, dict):
        servers = {}
    if "cartogate" in servers:
        report.skip(f"{rel} already registers a cartogate server")
        return
    servers["cartogate"] = {"command": MCP_COMMAND}
    data[key] = servers
    _write_json(path, data, report)
    report.did(f"{rel}  ({key}.cartogate → {MCP_COMMAND})")


def _ensure_mcp_codex(root: Path, report: _Report) -> None:
    """Register cartogate in the project ``.codex/config.toml`` (Codex uses TOML, not JSON).

    Merge-safe + idempotent: skips if a ``[mcp_servers.cartogate]`` table already exists, else
    appends the table (a new table header is always valid TOML), re-parsing the result to be sure we
    didn't corrupt an existing file before writing. Codex honors a project config only for TRUSTED
    projects, so we note that — the server won't appear until the folder is trusted in Codex.
    """
    rel = ".codex/config.toml"
    path = root / rel
    existing = _read_text(path)
    try:
        data = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError:
        report.note(f"{rel} isn't valid TOML — add [mcp_servers.cartogate] (command = "
                    f"\"{MCP_COMMAND}\") manually")
        return
    servers = data.get("mcp_servers")
    if isinstance(servers, dict) and "cartogate" in servers:
        report.skip(f"{rel} already registers a cartogate server")
        return
    block = f'[mcp_servers.cartogate]\ncommand = "{MCP_COMMAND}"\n'
    body = (existing.rstrip("\n") + "\n\n" + block) if existing.strip() else block
    try:
        tomllib.loads(body)  # never write a file we'd corrupt (e.g. an inline mcp_servers table)
    except tomllib.TOMLDecodeError:
        report.note(f"couldn't safely merge into {rel} — add [mcp_servers.cartogate] manually")
        return
    report.did(f"{rel}  (mcp_servers.cartogate → {MCP_COMMAND})")
    if not report.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        report.note("Codex reads a project .codex/config.toml only for TRUSTED projects — trust "
                    "this folder in Codex if cartogate doesn't appear.")


def _write_rule(root: Path, rel: str, report: _Report) -> None:
    """Write our agent-specific rule file (a cartogate-owned file — safe to (re)write).

    The frontmatter is what makes the rule ALWAYS-ON; a rule that has the content but predates the
    frontmatter is rewritten (it was silently manual-only).
    """
    path = root / rel
    frontmatter = _RULE_FRONTMATTER.get(rel, "")
    existing = _read_text(path)
    # The idempotency marker is the ACTIVATION line (trigger:/alwaysApply: true) — a rule someone
    # flipped to alwaysApply: false must be rewritten, not skipped as current.
    markers = [ln for ln in frontmatter.splitlines() if "always" in ln.lower()]
    marker = markers[0] if markers else ""
    # "On a BLOCK:" is the content-version check: a rule predating the block-recovery paragraph
    # is stale and gets rewritten (cartogate-owned file — safe).
    if (
        "set_workspace" in existing
        and "On a BLOCK:" in existing
        and (not marker or marker in existing)
    ):
        report.skip(f"{rel} already has the Cartogate workspace rule")
        return
    if not report.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter + _rule_text(), encoding="utf-8")
    report.did(f"{rel}  (always-on rule nudge + workspace)")


def _append_agents_md(root: Path, report: _Report) -> None:
    """Add the rule to AGENTS.md (the cross-agent convention). Appends only the workspace section if
    an older nudge is already there, else the full rule — never duplicating, always idempotent."""
    path = root / "AGENTS.md"
    existing = _read_text(path)
    if "set_workspace" in existing and "On a BLOCK:" in existing:
        report.skip("AGENTS.md already has the Cartogate workspace rule")
        return
    if "set_workspace" in existing:
        # Pre-block-recovery install: upgrade by appending just the missing paragraph.
        addition = "\n" + _BLOCK_RECOVERY
    elif "check_duplicate" in existing:
        addition = _workspace_section() + "\n" + _BLOCK_RECOVERY
    else:
        addition = _rule_text()
    if not report.dry_run:
        body = (existing.rstrip("\n") + "\n\n" + addition) if existing else addition
        path.write_text(body, encoding="utf-8")
    report.did("AGENTS.md  (rule nudge + workspace)")


WRITE_GATE_COMMAND = "cartogate-write-gate"
GREP_NUDGE_COMMAND = "cartogate-grep-nudge"
STOP_GATE_COMMAND = "cartogate-stop-gate"

#: Devin CLI's file-edit tool names (regex on the PreToolUse event's tool_name). Devin's exact
#: built-in tool names aren't published; this superset covers the common agent conventions. The
#: write gate FAILS OPEN on an unrecognized payload and the git pre-commit hook is the
#: fail-closed backstop, so a matcher miss degrades safely to commit-time enforcement. VERIFY
#: against a live Devin session and tighten to the real names.
_DEVIN_EDIT_MATCHER = (
    "Write|Edit|MultiEdit|str_replace|create_file|edit_file|write_file|apply_patch"
)


def _pretooluse_entries(entries: object, matcher: str, command: str) -> list[object] | None:
    """The shared find-or-append for a ``{matcher, hooks:[{type, command}]}`` PreToolUse entry.

    Returns the updated entry list, or ``None`` when ``command`` is already wired (the caller
    skips). Merge-safe by construction: existing entries are preserved, ours is appended.
    (Task #39 — three installers had grown identical copies of this shape.)
    """
    out = list(entries) if isinstance(entries, list) else []
    if any(command in json.dumps(e) for e in out):
        return None
    out.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
    return out


def _install_hook_claude(
    root: Path, report: _Report, *, matcher: str, command: str, label: str
) -> None:
    """Wire a PreToolUse command into ``.claude/settings.json`` — merge-safe, idempotent.

    Claude nests the event map under a ``hooks`` key; a malformed (non-object) value is
    announced before being replaced. Existing hooks are preserved.
    """
    rel = ".claude/settings.json"
    path = root / rel
    data = _read_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:  # a malformed (already-invalid) value — say so before replacing it
            report.note(
                f"{rel} 'hooks' was not an object ({type(hooks).__name__}) — replacing"
            )
        hooks = {}
    entries = _pretooluse_entries(hooks.get("PreToolUse"), matcher, command)
    if entries is None:
        report.skip(f"{rel} already wires the {label}")
        return
    hooks["PreToolUse"] = entries
    data["hooks"] = hooks
    _write_json(path, data, report)
    report.did(f"{rel}  (PreToolUse {label} → {command})")


def _install_write_hook_claude(root: Path, report: _Report) -> None:
    """The write-time gate for Claude Code (PreToolUse on file edits)."""
    _install_hook_claude(
        root, report, matcher="Write|Edit|MultiEdit",
        command=WRITE_GATE_COMMAND, label="write gate",
    )


def _install_grep_nudge_claude(root: Path, report: _Report) -> None:
    """The advisory grep nudge for Claude Code (PreToolUse on Grep)."""
    _install_hook_claude(
        root, report, matcher="Grep", command=GREP_NUDGE_COMMAND, label="grep nudge",
    )


def _install_write_hook_devin(root: Path, report: _Report) -> None:
    """Wire the write gate into ``.devin/hooks.v1.json`` (PreToolUse) — merge-safe, idempotent.

    Devin CLI hooks use the Claude-Code hook format, but the standalone ``.devin/hooks.v1.json``
    puts the event map at the TOP LEVEL (no ``hooks`` wrapper key). Blocking is exit code 2 — the
    same contract ``cartogate-write-gate`` already implements.
    """
    rel = ".devin/hooks.v1.json"
    path = root / rel
    data = _read_json(path)
    entries = _pretooluse_entries(data.get("PreToolUse"), _DEVIN_EDIT_MATCHER, WRITE_GATE_COMMAND)
    if entries is None:
        report.skip(f"{rel} already wires the write gate")
        return
    data["PreToolUse"] = entries
    _write_json(path, data, report)
    report.did(f"{rel}  (PreToolUse write gate → {WRITE_GATE_COMMAND})")


def _stop_entries(entries: object, command: str) -> list[object] | None:
    """Find or append a matcherless Stop entry invoking ``command``.

    Copy-then-append semantics (same shape as the PreToolUse installers): ``None`` when
    ``command`` is already wired (caller skips); else the updated entry list. Stop hooks
    take no matcher — they fire on every session end.
    """
    if not isinstance(entries, list):
        entries = []
    if any(command in json.dumps(e) for e in entries):
        return None  # Already present
    # Append a matcherless entry (no "matcher" key — the Stop hook doesn't filter by tool)
    entries.append({"hooks": [{"type": "command", "command": command}]})
    return entries


def _install_stop_hook_claude(root: Path, report: _Report) -> None:
    """Wire the stop-gate into ``.claude/settings.json`` Stop hook — merge-safe, idempotent.

    The Stop hook fires on session end; ours is appended only if none already invokes the
    stop-gate command.
    """
    rel = ".claude/settings.json"
    path = root / rel
    data = _read_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            report.note(
                f"{rel} 'hooks' was not an object ({type(hooks).__name__}) — replacing"
            )
        hooks = {}
    entries = hooks.get("Stop")
    if not isinstance(entries, list):
        entries = []
    new_entries = _stop_entries(entries, STOP_GATE_COMMAND)
    if new_entries is None:
        report.skip(f"{rel} already wires the stop-gate")
        return
    hooks["Stop"] = new_entries
    data["hooks"] = hooks
    _write_json(path, data, report)
    report.did(f"{rel}  (Stop hook → {STOP_GATE_COMMAND})")


def _install_stop_hook_devin(root: Path, report: _Report) -> None:
    """Wire the stop-gate into ``.devin/hooks.v1.json`` Stop hook — merge-safe, idempotent.

    Devin CLI hooks put the event map at the TOP LEVEL (no ``hooks`` wrapper key).
    """
    rel = ".devin/hooks.v1.json"
    path = root / rel
    data = _read_json(path)
    entries = data.get("Stop")
    if not isinstance(entries, list):
        entries = []
    new_entries = _stop_entries(entries, STOP_GATE_COMMAND)
    if new_entries is None:
        report.skip(f"{rel} already wires the stop-gate")
        return
    data["Stop"] = new_entries
    _write_json(path, data, report)
    report.did(f"{rel}  (Stop hook → {STOP_GATE_COMMAND})")


def _install_precommit(root: Path, force: bool, report: _Report) -> None:
    if not (root / ".git").exists():
        report.note("not a git repo — skipping the pre-commit hook")
        return
    hook = root / ".git" / "hooks" / "pre-commit"
    if hook.exists() and not force:
        existing = _read_text(hook)
        if "cartogate-precommit" in existing:
            report.skip(".git/hooks/pre-commit already runs Cartogate")
            return
        if "cartogate" not in existing:
            report.note(".git/hooks/pre-commit exists — rerun with --force to replace it")
            return
        # Ours, but pre-interpreter-pinning (bare `python -m`) — stale and BROKEN on pipx
        # machines; rewrite it (cartogate-owned file).
    if not report.dry_run:
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(_precommit_hook_text(), encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    report.did(".git/hooks/pre-commit  (commit-time duplicate gate)")


def _start_daemon(repo: Path, report: _Report) -> None:
    """Start a detached resolved daemon so Cartogate tools serve a warm, shared graph (and the F-09
    snapshot gets persisted). It survives the editor (job-breakaway) and is idempotent — a running
    daemon is left alone. This is what makes `set_workspace` land on a warm daemon, not a rebuild.
    """
    if not (repo / ".git").exists():
        report.note("not a git repo — skipping the warm daemon")
        return
    if report.dry_run:
        report.did("start a resolved daemon (warm shared graph for the tools)")
        return
    from cartogate.daemon.cli import cmd_start
    from cartogate.daemon.discovery import is_pid_alive, read_discovery
    from cartogate.daemon.registry import register_workspace

    register_workspace(repo)  # so a fresh session can auto-connect before any agent involvement
    existing = read_discovery(repo)
    if existing is not None and is_pid_alive(existing.pid):
        if existing.resolve:
            report.skip("a resolved cartogate daemon is already running for this repo")
        else:
            # A structural-only daemon can't serve the resolved tools, and cmd_start won't start a
            # second one alongside it — say so instead of silently leaving the warm path off.
            report.note(
                "a structural daemon is running for this repo — stop it (`cartogate daemon stop`) "
                "and re-run init to get the warm resolved daemon the tools use"
            )
        return
    cmd_start(repo, resolve=True, detach=True)  # detached (breakaway); builds + persists snapshot
    report.did("started a resolved daemon (warm shared graph for the tools)")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


#: Per-agent MCP config: (marker key) -> (relative path, JSON key). `devin` uniquely has BOTH a
#: file target (Devin CLI's project .devin/config.json) and a printed note (Devin Desktop's MCP
#: config is global) — the note is emitted from the run() loop, not this dict.
_MCP_TARGETS = {
    "claude": (".mcp.json", "mcpServers"),
    "cursor": (".cursor/mcp.json", "mcpServers"),
    "vscode": (".vscode/mcp.json", "servers"),  # VS Code uses `servers`, not `mcpServers`
    "devin": (".devin/config.json", "mcpServers"),  # Devin CLI: project stdio MCP config
}
_RULE_TARGETS = {
    "cursor": ".cursor/rules/cartogate.mdc",
    "devin": ".devin/rules/cartogate.md",  # Devin Desktop workspace rule
}

#: Frontmatter per rule target. WITHOUT it the editor defaults the rule to MANUAL activation and
#: it never fires — which silently disabled the whole workspace/tool nudge (user-reported).
_RULE_FRONTMATTER = {
    ".devin/rules/cartogate.md": "---\ntrigger: always_on\n---\n\n",
    ".cursor/rules/cartogate.mdc": (
        "---\ndescription: Cartogate code-graph tools and workspace rule\n"
        "alwaysApply: true\n---\n\n"
    ),
}


def run(
    root: Path,
    *,
    agents: set[str],
    dry_run: bool,
    force: bool,
    run_doctor: bool,
    start_daemon: bool = True,
    install_gate: bool = False,
) -> int:
    """Set up Cartogate in ``repo``.

    Default (``install_gate=False``): the *advisory* setup — MCP server config so the tools work,
    plus the warm daemon. It writes nothing INTO the repo (no rule files, no AGENTS.md) and installs
    no commit gate. ``install_gate=True`` (the caller passed ``--agent``) adds the opinionated
    surfaces: the per-tool rule nudge + AGENTS.md and the blocking commit-time duplicate gate.

    Reconfigures stdout to UTF-8 on entry (the report contains ``—`` / ``→``) so it doesn't crash a
    Windows cp1252 console — matching the other Cartogate CLIs.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the report uses — / →; avoid a cp1252 crash
    repo = (find_repo_root(root.resolve()) or root).resolve()
    report = _Report(dry_run)
    print(f"cartogate init — {repo}" + ("  (dry run)" if dry_run else ""))
    print(f"agents: {', '.join(sorted(agents))}\n")

    # A. MCP server config, per agent (always — it's what makes the tools available).
    for agent in sorted(agents):
        if agent in _MCP_TARGETS:
            rel, key = _MCP_TARGETS[agent]
            _ensure_mcp(repo, rel, key, report)
        if agent == "codex":
            _ensure_mcp_codex(repo, report)  # Codex uses a project .codex/config.toml (TOML)
        if agent == "devin":
            report.note(
                "Devin Desktop MCP config is global — add cartogate to "
                "~/.codeium/windsurf/mcp_config.json (command: cartogate-mcp). "
                "See docs/INTEGRATIONS.md."
            )

    # B + C. The opinionated surfaces — rule nudge (repo files) + the blocking commit gate — only
    # when a tool was explicitly named (`--agent`). The default init stays advisory / non-invasive.
    if install_gate:
        _append_agents_md(repo, report)  # B: rule nudge — AGENTS.md + per-tool rule files
        for agent in sorted(agents):
            if agent in _RULE_TARGETS:
                _write_rule(repo, _RULE_TARGETS[agent], report)
        _install_precommit(repo, force, report)  # C: commit-time hard gate
        # C2: the write-time gate — the only enforcement layer that acts IN the agent's loop
        # (STRATEGY.md law 2). One installed command, wired per harness.
        if "claude" in agents:
            _install_write_hook_claude(repo, report)
            _install_grep_nudge_claude(repo, report)
            _install_stop_hook_claude(repo, report)
        if "devin" in agents:
            _install_write_hook_devin(repo, report)
            _install_stop_hook_devin(repo, report)
        if "codex" in agents:
            report.note(
                "Codex has no pre-write hook surface — the commit-time gate above is the "
                "enforcement layer there."
            )
    else:
        report.note(
            "advisory setup (MCP + daemon) — no rules written, no commit gate. Re-run with "
            "--agent <tool> (e.g. --agent devin) to add the rule nudge + the commit gate."
        )

    # D. Warm daemon — so the first set_workspace / tool call lands on a warm shared graph.
    if start_daemon:
        _start_daemon(repo, report)
    if not dry_run:
        from cartogate.daemon.discovery import ensure_state_dir

        ensure_state_dir(repo)  # .cartogate exists and is self-ignoring from day one

    if not dry_run:  # the summary reads CURRENT state — misleading under "would write" lines
        _print_surfaces_summary(repo, agents)

    if not report.changed:
        # In advisory mode the note above already summarizes the state + how to opt in — a bare
        # "nothing to do" there reads as contradictory, so only say it in full-adoption mode.
        if install_gate:
            print("\nCartogate is already set up here — nothing to do.")
    elif dry_run:
        print("\nDry run — nothing was written. Re-run without --dry-run to apply.")

    if run_doctor and not dry_run:
        print()
        from cartogate.doctor import run as doctor_run

        return doctor_run(repo)
    return 0


def _print_surfaces_summary(repo: Path, agents: set[str]) -> None:
    """A clear closing statement of WHAT IS ACTIVE — the wrote/skip stream above says what changed
    this run, but not what the repo's Cartogate posture actually is (user-reported blind spot)."""
    from cartogate.daemon.discovery import is_pid_alive, read_discovery

    print("\nactive surfaces:")
    for agent in sorted(agents):
        rel = _RULE_TARGETS.get(agent)
        if rel is None:
            continue
        text = _read_text(repo / rel)
        # The always-on marker sits on a different frontmatter line per editor (windsurf:
        # trigger line 1; cursor: alwaysApply line 2) — scan the header block, counting only
        # ACTIVE values (an alwaysApply: false must not read as always-on).
        header = [ln.strip().lower() for ln in text.splitlines()[:5]]
        active = any(ln in ("trigger: always_on", "alwaysapply: true") for ln in header)
        if "set_workspace" not in text:
            print(f"  rule ({agent}): NOT installed — run with --agent {agent}")
        elif active:
            print(f"  rule ({agent}): {rel} (always-on)")
        else:
            print(f"  rule ({agent}): {rel} (NO always-on frontmatter — re-run init to upgrade)")
    write_hook_files = {
        "claude": ".claude/settings.json",
        "devin": ".devin/hooks.v1.json",
    }
    for agent in sorted(agents & write_hook_files.keys()):
        rel = write_hook_files[agent]
        wired = WRITE_GATE_COMMAND in _read_text(repo / rel)
        state = "installed" if wired else "NOT installed — run with --agent " + agent
        print(f"  write gate ({agent}): {state}")
    hook = _read_text(repo / ".git" / "hooks" / "pre-commit")
    print(f"  commit gate: {'installed' if 'cartogate' in hook else 'NOT installed'}"
          f"{'' if 'cartogate' in hook else ' — run with --agent <tool>'}")
    info = read_discovery(repo)
    if info is not None and is_pid_alive(info.pid):
        kind = "resolved" if info.resolve else "structural"
        print(f"  daemon: running ({kind}, pid {info.pid}, port {info.port})")
    else:
        print("  daemon: not running")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cartogate init",
        description="Set up Cartogate here: MCP config + warm daemon by default; --agent <tool> "
        "also writes that tool's rule nudge and installs the commit gate.",
    )
    parser.add_argument("root", nargs="?", default=".", help="repo to set up (default: cwd)")
    parser.add_argument(
        "--agent",
        action="append",
        choices=["claude", "cursor", "windsurf", "devin", "vscode", "codex", "all"],
        help="adopt a specific tool (repeatable): writes its rule nudge + the commit gate. Without "
        "it, init is advisory (MCP + daemon only) for the auto-detected tools.",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without writing anything")
    parser.add_argument("--force", action="store_true", help="replace an existing pre-commit hook")
    parser.add_argument("--no-doctor", action="store_true", help="skip the closing health check")
    parser.add_argument("--no-daemon", action="store_true", help="don't start the warm daemon")
    ns = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if ns.agent and "windsurf" in ns.agent:
        print(
            "note: --agent windsurf is deprecated; use --agent devin "
            "(Windsurf is now Devin Desktop)"
        )
        ns.agent = ["devin" if a == "windsurf" else a for a in ns.agent]

    root = Path(ns.root)
    all_agents = {"claude", "cursor", "devin", "vscode", "codex"}
    # Explicitly naming a tool (`--agent`) opts into the opinionated surfaces (rules + commit gate);
    # without it, init is advisory (MCP + daemon) for the auto-detected tools.
    install_gate = bool(ns.agent)
    if ns.agent:
        agents = all_agents if "all" in ns.agent else set(ns.agent)
    else:
        # Auto-detect; if the repo has no agent markers at all, wire the common Claude/`.mcp.json`
        # convention so an MCP client still picks it up.
        agents = detect_agents(find_repo_root(root.resolve()) or root) or {"claude"}

    return run(
        root,
        agents=agents,
        dry_run=ns.dry_run,
        force=ns.force,
        run_doctor=not ns.no_doctor,
        start_daemon=not ns.no_daemon,
        install_gate=install_gate,
    )


if __name__ == "__main__":
    raise SystemExit(main())
