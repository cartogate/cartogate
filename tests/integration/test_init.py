"""`cartogate init` — one-command adoption: MCP config + rule nudge + commit gate, idempotent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from cartogate.init_cmd import detect_agents, run


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)


def test_detect_agents_from_markers(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "CLAUDE.md").write_text("", encoding="utf-8")
    assert detect_agents(tmp_path) == {"cursor", "claude", "codex"}


def test_init_writes_mcp_rule_and_hook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    (tmp_path / ".vscode").mkdir()

    code = run(
        tmp_path, agents={"cursor", "vscode"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,  # --agent path: write rules + the commit gate
    )
    assert code == 0

    # MCP config: Cursor uses `mcpServers`, VS Code uses `servers` — both point at cartogate-mcp.
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert cursor["mcpServers"]["cartogate"]["command"] == "cartogate-mcp"
    vscode = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert vscode["servers"]["cartogate"]["command"] == "cartogate-mcp"

    # Rule nudge + commit-time hook.
    assert "check_duplicate" in (tmp_path / "AGENTS.md").read_text()
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert "cartogate.precommit" in hook.read_text()


def test_init_rule_has_set_workspace_and_no_hardcoded_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )

    rule = (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(encoding="utf-8")
    assert "set_workspace" in rule  # tells the agent the recovery step
    assert "check_duplicate" in rule  # and keeps the tool-usage nudge
    assert "On a BLOCK:" in rule  # explicit block-recovery rule (GPT-5.x literalism)
    assert "Never retry the identical call" in rule
    # Machine-independent: NO absolute path baked in, so the rule is committable/shareable.
    assert str(tmp_path.resolve()) not in rule
    assert "AppData" not in rule and "/Users/" not in rule
    assert "set_workspace" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    # Idempotent: re-running writes nothing new.
    before = rule
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(encoding="utf-8") == before


def test_rule_files_carry_always_on_frontmatter(tmp_path: Path) -> None:
    """Without frontmatter the editor defaults the rule to MANUAL and it never fires — the rule
    must open with the always-on header (user-reported: the agent kept 'forgetting' the rule)."""
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    (tmp_path / ".cursor").mkdir()
    run(tmp_path, agents={"windsurf", "cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    ws = (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(encoding="utf-8")
    assert ws.startswith("---\ntrigger: always_on\n---\n")
    cur = (tmp_path / ".cursor" / "rules" / "cartogate.mdc").read_text(encoding="utf-8")
    assert cur.startswith("---\n") and "alwaysApply: true" in cur.splitlines()[2]
    assert "set_workspace" in ws and "set_workspace" in cur  # content intact under the header


def test_old_frontmatterless_rule_is_upgraded(tmp_path: Path) -> None:
    """A rule written by an older init (right content, NO frontmatter -> silently manual) must be
    REWRITTEN, not skipped."""
    from cartogate.init_cmd import _rule_text

    _git_init(tmp_path)
    (tmp_path / ".windsurf" / "rules").mkdir(parents=True)
    (tmp_path / ".windsurf" / "rules" / "cartogate.md").write_text(
        _rule_text(), encoding="utf-8"  # pre-frontmatter era content
    )
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    upgraded = (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(encoding="utf-8")
    assert upgraded.startswith("---\ntrigger: always_on\n---\n")
    # And idempotent afterwards.
    before = upgraded
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(
        encoding="utf-8") == before


def test_disabled_cursor_rule_is_rewritten_not_skipped(tmp_path: Path) -> None:
    """A cursor rule flipped to alwaysApply: false (right content, deactivated) must be
    rewritten on the next init — the idempotency marker is the ACTIVATION line."""
    from cartogate.init_cmd import _rule_text

    _git_init(tmp_path)
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    disabled = (
        "---" + chr(10) + "description: Cartogate code-graph tools and workspace rule"
        + chr(10) + "alwaysApply: false" + chr(10) + "---" + chr(10) + chr(10) + _rule_text()
    )
    (rules / "cartogate.mdc").write_text(disabled, encoding="utf-8")
    run(tmp_path, agents={"cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    fixed = (rules / "cartogate.mdc").read_text(encoding="utf-8")
    assert "alwaysApply: true" in fixed and "alwaysApply: false" not in fixed


def test_init_prints_the_active_surfaces_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    out = capsys.readouterr().out
    assert "active surfaces:" in out
    assert "rule (windsurf): .windsurf/rules/cartogate.md (always-on)" in out
    assert "commit gate: installed" in out
    assert "daemon: not running" in out  # start_daemon=False in tests


def test_summary_reports_cursor_rule_always_on_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Cursor's alwaysApply sits on frontmatter line 2, not line 1 — the summary must scan the
    # whole header block or it lies for cursor users.
    _git_init(tmp_path)
    (tmp_path / ".cursor").mkdir()
    run(tmp_path, agents={"cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    out = capsys.readouterr().out
    assert "rule (cursor): .cursor/rules/cartogate.mdc (always-on)" in out


def test_rule_predating_block_recovery_is_upgraded(tmp_path: Path) -> None:
    """A rule with the right workspace content but no "On a BLOCK:" paragraph (pre-overhaul
    install) is stale — rewritten, then idempotent."""
    from cartogate.init_cmd import _rule_text

    _git_init(tmp_path)
    (tmp_path / ".windsurf" / "rules").mkdir(parents=True)
    # Simulate the pre-overhaul rule text (frontmatter + content, no block-recovery paragraph).
    old_rule = "---\ntrigger: always_on\n---\n\n" + _rule_text().replace(
        "**On a BLOCK:**", "**Old:**"
    )
    (tmp_path / ".windsurf" / "rules" / "cartogate.md").write_text(old_rule, encoding="utf-8")
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    upgraded = (tmp_path / ".windsurf" / "rules" / "cartogate.md").read_text(encoding="utf-8")
    assert "On a BLOCK:" in upgraded


def test_agents_md_predating_block_recovery_gains_only_the_paragraph(tmp_path: Path) -> None:
    from cartogate.init_cmd import _rule_text

    _git_init(tmp_path)
    old = _rule_text().replace("**On a BLOCK:**", "**Old:**")
    (tmp_path / "AGENTS.md").write_text(old, encoding="utf-8")
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "On a BLOCK:" in text
    assert text.count("## Workspace") == 1  # upgraded by APPENDING the paragraph, not duplicated
    before = text
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == before  # then idempotent


def test_precommit_hook_pins_the_interpreter(tmp_path: Path) -> None:
    """FIELD BUG (2026-07-04): the hook ran bare `python -m cartogate.precommit`; on a machine
    where PATH's python lacks cartogate the fail-closed backstop ERRORED and the agent bypassed
    with --no-verify. The hook must prefer the console script and fall back to the EXACT
    interpreter that ran init — never an unpinned `python`."""
    import sys as _sys
    from pathlib import PurePosixPath

    _git_init(tmp_path)
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    hook = (tmp_path / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "cartogate-precommit" in hook  # PATH-independent console script first
    pinned = PurePosixPath(_sys.executable.replace(chr(92), "/"))
    assert f'"{pinned}"' in hook  # quoted absolute fallback interpreter
    assert "\nexec python -m cartogate.precommit" not in hook  # the bare form is gone


def test_old_precommit_hook_is_upgraded(tmp_path: Path) -> None:
    _git_init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n# Cartogate duplicate-signature gate — installed by `cartogate init`.\n"
        "exec python -m cartogate.precommit\n",
        encoding="utf-8",
    )
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    upgraded = hook.read_text(encoding="utf-8")
    assert "cartogate-precommit" in upgraded  # stale cartogate-owned hook was rewritten
    before = upgraded
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert hook.read_text(encoding="utf-8") == before  # then idempotent


def test_foreign_precommit_hook_is_not_clobbered(tmp_path: Path) -> None:
    _git_init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexec my-own-linter\n", encoding="utf-8")
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert "my-own-linter" in hook.read_text(encoding="utf-8")  # untouched without --force


def test_init_claude_wires_the_write_gate(tmp_path: Path) -> None:
    """--agent claude installs the PreToolUse write gate into .claude/settings.json,
    merge-safe (existing hooks preserved) and idempotent."""
    _git_init(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-hook"}]}
        ]},
        "other": {"keep": True},
    }), encoding="utf-8")
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    data = json.loads(settings.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    assert any("my-own-hook" in json.dumps(e) for e in entries)  # merge-safe
    ours = [e for e in entries if "cartogate-write-gate" in json.dumps(e)]
    assert len(ours) == 1
    assert ours[0]["matcher"] == "Write|Edit|MultiEdit"
    assert data["other"] == {"keep": True}
    before = settings.read_text(encoding="utf-8")
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert settings.read_text(encoding="utf-8") == before  # idempotent


def test_init_windsurf_wires_the_write_gate(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    hooks = json.loads((tmp_path / ".windsurf" / "hooks.json").read_text(encoding="utf-8"))
    entries = hooks["pre_write_code"]
    assert any("cartogate-write-gate" in json.dumps(e) for e in entries)
    before = json.dumps(hooks)
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    after = json.loads((tmp_path / ".windsurf" / "hooks.json").read_text(encoding="utf-8"))
    assert json.dumps(after) == before  # idempotent


def test_init_without_agent_does_not_touch_hook_configs(tmp_path: Path) -> None:
    _git_init(tmp_path)
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,  # no install_gate: advisory mode
    )
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_dry_run_does_not_touch_write_gate_configs(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    (tmp_path / ".claude").mkdir()
    run(tmp_path, agents={"claude", "windsurf"}, dry_run=True, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".windsurf" / "hooks.json").exists()


def test_malformed_hooks_value_is_replaced_with_a_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": [], "other": True}), encoding="utf-8")
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    out = capsys.readouterr().out
    assert "'hooks' was not an object" in out  # the discard is announced, not silent
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["other"] is True  # the rest of the file is preserved
    assert any("cartogate-write-gate" in json.dumps(e) for e in data["hooks"]["PreToolUse"])


def test_summary_reports_the_write_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / ".windsurf").mkdir()
    run(tmp_path, agents={"windsurf"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    out = capsys.readouterr().out
    assert "write gate (windsurf): installed" in out


def test_init_makes_cartogate_dir_self_ignoring(tmp_path: Path) -> None:
    _git_init(tmp_path)
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,
    )
    ignore = tmp_path / ".cartogate" / ".gitignore"
    assert ignore.read_text(encoding="utf-8").strip() == "*"  # runtime state never hits git status


def test_init_upgrades_an_old_agents_md_without_duplicating(tmp_path: Path) -> None:
    from cartogate.init_cmd import RULE_NUDGE

    _git_init(tmp_path)
    (tmp_path / "AGENTS.md").write_text(RULE_NUDGE, encoding="utf-8")  # a pre-workspace nudge
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "set_workspace" in text  # the workspace step was appended
    assert text.count("Gate (can BLOCK)") == 1  # ...and the old nudge was NOT duplicated
    before = text
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == before  # idempotent


def test_init_codex_registers_toml_mcp_and_is_idempotent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    # Codex uses a project TOML config, and its rule is AGENTS.md (no per-tool rule file).
    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["cartogate"]["command"] == "cartogate-mcp"
    assert "set_workspace" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()  # commit gate

    before = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    assert (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8") == before  # idempotent


def test_init_codex_mcp_is_merge_safe(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8"
    )
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    cfg = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["mcp_servers"]["other"]["command"] == "x"  # pre-existing server preserved
    assert cfg["mcp_servers"]["cartogate"]["command"] == "cartogate-mcp"  # cartogate added


def test_init_codex_leaves_a_malformed_config_untouched(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".codex").mkdir()
    bad = "this is not valid toml ][\n"
    (tmp_path / ".codex" / "config.toml").write_text(bad, encoding="utf-8")
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    # Unparseable existing config -> we don't touch it (no clobber; the note tells the user).
    assert (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8") == bad


def test_init_codex_does_not_corrupt_an_inline_mcp_servers_table(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / ".codex").mkdir()
    inline = 'mcp_servers = { other = { command = "x" } }\n'
    (tmp_path / ".codex" / "config.toml").write_text(inline, encoding="utf-8")
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,
    )
    # Appending a [mcp_servers.cartogate] table clashes with the inline table -> the re-parse guard
    # bails, leaving the file intact rather than writing something Codex can't parse.
    text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert text == inline
    tomllib.loads(text)  # still valid TOML


def test_init_is_merge_safe_and_idempotent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    # Pre-existing MCP config with an unrelated server must be preserved.
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )

    run(tmp_path, agents={"cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,
    )
    cfg = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert cfg["mcpServers"]["other"]["command"] == "x"  # preserved
    assert cfg["mcpServers"]["cartogate"]["command"] == "cartogate-mcp"  # added

    # Re-running changes nothing.
    before = (tmp_path / ".cursor" / "mcp.json").read_text()
    run(tmp_path, agents={"cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,
    )
    assert (tmp_path / ".cursor" / "mcp.json").read_text() == before


def test_init_starts_a_resolved_daemon_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    calls: list[tuple[Path, bool, bool]] = []
    monkeypatch.setattr(
        "cartogate.daemon.cli.cmd_start",
        lambda root, *, resolve=False, detach=False: calls.append((root, resolve, detach)) or 0,
    )
    # No daemon running -> init starts a detached resolved one.
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False)
    assert calls == [(tmp_path, True, True)]  # resolve=True, detach=True

    # Already running -> init leaves it alone (idempotent).
    from cartogate.daemon.discovery import DiscoveryInfo, write_discovery

    write_discovery(
        tmp_path,
        DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(), token="t",
                      repo=str(tmp_path), resolve=True),
    )
    calls.clear()
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False)
    assert calls == []  # not restarted


def test_init_does_not_start_a_second_daemon_over_a_structural_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    started: list[int] = []
    monkeypatch.setattr("cartogate.daemon.cli.cmd_start", lambda *a, **k: started.append(1) or 0)
    from cartogate.daemon.discovery import DiscoveryInfo, write_discovery

    # A live STRUCTURAL daemon (resolve=False) — init can't upgrade it in place, so it must NOT
    # claim success by starting a second one; it notes the situation instead.
    write_discovery(
        tmp_path,
        DiscoveryInfo(host="127.0.0.1", port=1, pid=os.getpid(), token="t",
                      repo=str(tmp_path), resolve=False),
    )
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False)
    assert started == []  # not started over the structural daemon


def test_init_registers_the_workspace_for_autoconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CARTOGATE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("cartogate.daemon.cli.cmd_start", lambda *a, **k: 0)
    _git_init(tmp_path)
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False)
    from cartogate.daemon.registry import registered_workspaces

    assert tmp_path.resolve() in registered_workspaces()  # first session can auto-connect


def test_init_no_daemon_flag_skips_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    started = []
    monkeypatch.setattr(
        "cartogate.daemon.cli.cmd_start",
        lambda *a, **k: started.append(1) or 0,
    )
    run(tmp_path, agents={"claude"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,
    )
    assert started == []


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    run(tmp_path, agents={"cursor", "codex"}, dry_run=True, force=False, run_doctor=False,
        start_daemon=False, install_gate=True,  # even the gate surfaces must be preview-only
    )
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".cursor" / "rules" / "cartogate.mdc").exists()  # rule file too
    assert not (tmp_path / ".codex" / "config.toml").exists()  # codex TOML too
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


def test_init_codex_advisory_writes_toml_but_no_rule_or_gate(tmp_path: Path) -> None:
    _git_init(tmp_path)
    run(tmp_path, agents={"codex"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,  # install_gate defaults False -> advisory
    )
    assert (tmp_path / ".codex" / "config.toml").exists()  # MCP config IS written
    assert not (tmp_path / "AGENTS.md").exists()  # ...but no rule
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()  # ...and no commit gate


def test_init_default_is_advisory_no_rules_or_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default init (no --agent -> install_gate=False): MCP config is written, but NO rules go into
    the repo and NO commit gate is installed. The opinionated surfaces are opt-in."""
    _git_init(tmp_path)
    (tmp_path / ".cursor").mkdir()
    run(tmp_path, agents={"cursor"}, dry_run=False, force=False, run_doctor=False,
        start_daemon=False,  # install_gate defaults False
    )
    assert (tmp_path / ".cursor" / "mcp.json").exists()  # MCP config IS written (tools available)
    assert not (tmp_path / ".cursor" / "rules" / "cartogate.mdc").exists()  # no rule file
    assert not (tmp_path / "AGENTS.md").exists()  # no AGENTS.md nudge
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()  # no commit gate
    assert "advisory setup" in capsys.readouterr().out  # ...and it tells the user how to opt in


def test_init_main_agent_flag_toggles_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cartogate.init_cmd import main

    monkeypatch.setattr("cartogate.daemon.cli.cmd_start", lambda *a, **k: 0)  # never spawn in tests
    (tmp_path / ".cursor").mkdir()
    _git_init(tmp_path)

    # Bare init (no --agent) -> advisory: MCP but no gate.
    main([str(tmp_path), "--no-doctor", "--no-daemon"])
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()

    # Naming a tool -> the gate is installed.
    main([str(tmp_path), "--agent", "cursor", "--no-doctor", "--no-daemon"])
    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()
    assert (tmp_path / ".cursor" / "rules" / "cartogate.mdc").exists()


def test_installed_precommit_blocks_a_duplicate(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def authenticate(n):\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def authenticate(n):\n    return 2\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "cartogate.precommit", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "duplicate" in result.stderr.lower()
