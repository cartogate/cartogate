# Integrating Cartogate with coding agents

> **Fast path:** `cartogate init --agent <windsurf|cursor|vscode|claude|codex>` wires everything
> on this page for you — MCP config, an always-on rule, the commit gate, and a warm daemon — and
> prints an `active surfaces` summary. This page documents what it sets up, and how to do any of
> it by hand.

Cartogate plugs into a coding agent through **three independent surfaces** — use as many as your
agent supports:

| Surface | What it gives you | Works with |
|---|---|---|
| **A. MCP server** | The 13 deterministic tools (`check_duplicate`, `blast_radius`, …) the agent can call while it works | Any MCP-capable agent |
| **B. Rule nudge** | A short always-on instruction telling the agent *when* to call the tools | Any agent that reads a rules/instructions file |
| **C. Hard gate** | A real BLOCK that refuses a duplicate/contract break — at write-time and/or commit-time | Claude Code / Codex / Windsurf (write-time) · **every** agent (commit-time) |

A good setup is **A + B + C**: the agent can query the graph (A), knows when to (B), and is
backstopped by a gate it can't skip (C).

> **The universal backstop (any agent, even none):** the git pre-commit hook. It is
> agent-agnostic and fails *closed*, so a duplicate that slips past the in-loop tools is still
> caught before it lands. It judges only what the commit INTRODUCES (pre-existing duplication is
> a one-line note, never a block), and the same run prints deterministic advisories — changed
> contracts with un-updated callers/tests/docs, weakened tests alongside source changes, newly
> introduced import cycles, deletions that still have references — plus a gate-coverage stamp
> that lets `cartogate stats`/`doctor` show which commits bypassed the gate.
>
> ```bash
> cp hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
> ```

---

## A. The MCP server (shared by every agent)

One local stdio process serves all 13 tools. It finds the project through a **resolution
ladder**, so nothing is hardcoded and multiple open projects each get their own graph:

1. an explicit pin (`cartogate-mcp <path>` or `CARTOGATE_REPO`) — optional;
2. the agent's own signal — the `workspace_root` parameter every tool accepts, or the
   `set_workspace` tool (the always-on rule teaches agents to send it);
3. the editor's MCP workspace roots, where the editor provides real ones;
4. the workspace registry — a previously-activated repo whose daemon is still running is an
   unambiguous anchor when it is the only one.

In practice: run `cartogate init` once per repo and subsequent editor sessions resolve
automatically.

Each agent registers that command in its own config. **Mind the JSON key** — it differs by agent:
most use `mcpServers`, but **VS Code uses `servers`**.

The canonical block (Claude Code / Cursor / Windsurf / Cline / most clients) — drop it in a
**project** config so it resolves the current repo:

```jsonc
{
  "mcpServers": {
    "cartogate": {
      "command": "cartogate-mcp"
    }
  }
}
```

`"command": "cartogate-mcp"` assumes the binary is on the **PATH the agent sees**. That's true when
Cartogate is installed with **pipx** (`pipx ensurepath` once). If you installed into a project venv
instead, give the **absolute path** to the binary (e.g.
`".../.venv/Scripts/cartogate-mcp.exe"` on Windows) — a GUI agent won't have your venv on PATH.

### Troubleshooting a "dead" MCP connection

- **It's a stdio server, not HTTP.** It talks JSON-RPC over stdin/stdout of the process the agent
  spawns — there is **no port**. `curl`/`Invoke-WebRequest` against it will *always* look dead;
  that's not a real signal. To test it, run the handshake the agent does (`cartogate-mcp` should
  start and sit waiting on stdin, not exit).
- **`cartogate doctor` does not test the MCP server** — it checks the gate/daemon/hooks. A green
  doctor says nothing about MCP. (It *does* now report whether the **MCP SDK** is installed and
  whether `cartogate-mcp` is importable — the first thing to check.)
- **Missing MCP SDK (rare now — it's a base dependency).** A *complete* install always has it. Only
  a partial/broken install would make `cartogate-mcp` print `cartogate-mcp needs the MCP SDK, which
  isn't installed` and exit non-zero (so an agent shows it dead). Fix as it says:
  `pipx inject cartogate 'mcp>=1.2,<2'`, or just reinstall.
- **First start indexes in the background.** The server answers the handshake immediately and builds
  the graph on connect; the *first tool call* may wait for that build on a large repo. The server is
  alive throughout — it is no longer marked dead by a slow initial index.
- **Daemon sharing (optional, automatic).** `cartogate-mcp` prefers a warm **resolved** daemon if one
  is running (`cartogate daemon start --resolve`), forwarding tool calls to it so the gate + MCP share
  one graph and the first query is instant. If none is up it serves in-process and starts one for next
  time. Pass **`--no-daemon`** (or set `CARTOGATE_NO_DAEMON=1`) to force the daemonless in-process path
  where a persistent background process is unwanted.

---

## B + C, per agent

Cartogate already ships rule files for the common conventions: **`CLAUDE.md`** (Claude Code),
**`AGENTS.md`** (the emerging cross-agent standard — Codex, Cursor, Amp, Jules, … read it), and
**`.windsurf/rules/cartogate.md`** (Windsurf). Copy/adapt the wording into whatever file your
agent expects.

### Claude Code  *(A + B + C — the fullest integration)*
- **MCP:** `claude mcp add cartogate -- cartogate-mcp` (or a project `.mcp.json` with the
  `mcpServers` block above; the server auto-detects the repo).
- **Rule nudge:** `CLAUDE.md` (already in this repo). Claude Code reads it automatically.
- **Hard gate (write-time):** `cartogate init --agent claude` wires it for you — a merge-safe
  `PreToolUse` entry in `.claude/settings.json` running the installed `cartogate-write-gate`
  command, so an edit that introduces a duplicate is blocked (exit 2) *before* it's written.
  By hand, the equivalent is:
  ```jsonc
  {
    "hooks": {
      "PreToolUse": [
        { "matcher": "Write|Edit|MultiEdit",
          "hooks": [ { "type": "command", "command": "cartogate-write-gate" } ] }
      ]
    }
  }
  ```

### OpenAI Codex CLI  *(A + B + C)*
- **MCP:** `cartogate init --agent codex` writes the project `.codex/config.toml` for you (merge-safe;
  Codex honors a project config only for **trusted** folders). Or do it by hand —
  `codex mcp add cartogate -- cartogate-mcp`, or add a table to `~/.codex/config.toml`
  (or a project `.codex/config.toml`):
  ```toml
  [mcp_servers.cartogate]
  command = "cartogate-mcp"
  ```
- **Rule nudge:** `AGENTS.md` (already in this repo) — Codex reads it as project instructions.
- **Hard gate:** Codex treats a tool-call hook exit 2 as a block (same `hooks/pretooluse_gate.py`,
  where supported); the git pre-commit hook is the guaranteed backstop.

### Cursor  *(A + B, + C via commit hook)*
- **MCP:** project `.cursor/mcp.json` (or global `~/.cursor/mcp.json`) with the `mcpServers` block.
- **Rule nudge:** `.cursor/rules/cartogate.mdc` (an always-apply rule), or rely on `AGENTS.md`,
  which Cursor also reads. Reuse the wording from this repo's `AGENTS.md`.
- **Hard gate:** Cursor Hooks (1.7+) have **no pre-write block** — `afterFileEdit` runs *after* an
  edit (so it can re-check / flag, not prevent), and `beforeMCPExecution` can deny MCP calls but
  not Cursor's own edits. So in-loop enforcement is advisory; the **git pre-commit hook is the hard
  gate**.

### Windsurf  *(A + B + C — write-time gate via Cascade Hooks)*
- **MCP:** add the `mcpServers` block via Windsurf's MCP config
  (`~/.codeium/windsurf/mcp_config.json` or the in-app **Manage MCP servers**).
- **Rule nudge:** `.windsurf/rules/cartogate.md` (already in this repo, `trigger: always_on`).
- **Hard gate (write-time):** `cartogate init --agent windsurf` wires it — a merge-safe
  `pre_write_code` entry in `.windsurf/hooks.json` running the installed `cartogate-write-gate`
  command. Windsurf's **Cascade Hooks** run it *before* Cascade edits a file and **block on exit
  code 2** — the same contract as Claude Code's `PreToolUse`. The one command auto-detects both
  payload shapes (Claude's `tool_input`, Windsurf's `tool_info`), so a duplicate is judged
  identically in either editor; an unrecognized payload fails OPEN (the git pre-commit hook is
  the fail-closed backstop).

### VS Code — GitHub Copilot (agent mode)  *(A + B, + C via commit hook)*
- **MCP:** project `.vscode/mcp.json` — **note the `servers` key, not `mcpServers`:**
  ```jsonc
  { "servers": { "cartogate": { "command": "cartogate-mcp" } } }
  ```
  MCP tools are available in Copilot **Agent mode** only.
- **Rule nudge:** `.github/copilot-instructions.md` (paste this repo's `AGENTS.md` wording).
- **Hard gate:** git pre-commit hook.

### Zed  *(A + B, + C via commit hook)*
- **MCP:** add a context server in `settings.json`:
  ```jsonc
  { "context_servers": { "cartogate": {
      "command": { "path": "cartogate-mcp", "args": [], "env": {} } } } }
  ```
- **Rule nudge:** a `.rules` file (Zed reads `AGENTS.md`-style rule files).
- **Hard gate:** git pre-commit hook.

### Cline / Roo Code (VS Code extensions)  *(A + B, + C via commit hook)*
- **MCP:** the extension's `cline_mcp_settings.json` (or **MCP Servers → Configure**), `mcpServers`
  block above.
- **Rule nudge:** a `.clinerules` / `.roorules` file — reuse the `AGENTS.md` wording.
- **Hard gate:** git pre-commit hook.

### JetBrains AI Assistant / Junie  *(A + B, + C via commit hook)*
- **MCP:** **Settings → Tools → AI Assistant → Model Context Protocol (MCP)** → add the `mcpServers`
  block (or import from an existing JSON).
- **Rule nudge:** project guidelines / `AGENTS.md`.
- **Hard gate:** git pre-commit hook.

### Any other MCP client
Register `cartogate-mcp` as a local **stdio** MCP server (the `mcpServers` block above, or
`servers` for VS Code-family clients), drop the `AGENTS.md` wording wherever the agent reads
project instructions, and install the git pre-commit hook as the backstop.

---

## What the agent gets (the 13 tools)

`check_duplicate` is the only tool that can **BLOCK**; the rest are **advisory** (they never
block). See [`AGENTS.md`](../AGENTS.md) for the ready-to-use rule wording and the
[README](../README.md#the-tools) for the full tool table with one-line descriptions.

**Languages.** The gate and the graph cover Python, TypeScript, JavaScript, Java, Go, Rust, C#, C,
C++, Kotlin, and Swift — the duplicate gate fires per-file-language, so it works the same across all
of them.

**Measuring the value.** To A/B the integration on a real agent, see the manual tactical protocol
in [`evaluation/windsurf_ab/PROTOCOL.md`](../evaluation/windsurf_ab/PROTOCOL.md) — it scores duplicate-prevention with a
deterministic oracle and tracks token/step efficiency across a Cartogate-on vs. Cartogate-off run.
