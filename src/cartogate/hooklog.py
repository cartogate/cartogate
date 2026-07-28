"""Hook firing log — ``cartogate-hook log --source <tag>`` (Devin Desktop instrumentation).

Devin Desktop hosts two agents (Cascade and Devin Local) whose hook surfaces are in flux:
the documented config locations/formats lag the product, and the file-edit tool names are
unpublished. Rather than guess, ``cartogate init --agent devin`` shotgun-installs hook
entries across EVERY plausible location, each invoking this dispatcher, which appends one
JSONL line per firing to ``.cartogate/hooklog.jsonl``. ``cartogate hooks status`` then
cross-references the log against the installed entries — turning "which surfaces does this
build actually read?" into an empirical question answered by normal IDE use.

Two invariants, both load-bearing:

- **Never disturb the agent.** Exit 0 always — on unparseable payloads, on log-write
  failures, on miswired argv. A logging hook that errors (or blocks) trains the agent to
  route around Cartogate (STRATEGY.md law 3).
- **Log provenance, not content.** Lines carry the source tag, payload dialect, event/tool
  names, file path, and session id — never code, prompts, or command lines.

The payload dialect doubles as the agent fingerprint: Cascade speaks
``agent_action_name``/``tool_info``; Devin Local / Devin CLI / Claude Code speak
``hook_event_name``/``tool_name``/``tool_input``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cartogate.surfaces import find_repo_root

#: Repo-relative path of the firing log (inside the self-ignoring state dir — locally
#: visible to the user, never committed).
LOG_REL = ".cartogate/hooklog.jsonl"


def detect(payload: object) -> dict[str, Any]:
    """Classify a hook payload's dialect and extract its provenance fields.

    Returns a partial record: ``dialect`` plus whichever of ``event``/``tool``/``file``/
    ``session``/``keys`` apply. Unknown dict shapes keep their top-level key names so a new
    schema is diagnosable from the log alone.
    """
    if not isinstance(payload, dict):
        return {"dialect": "unparseable"}
    if "agent_action_name" in payload:  # Cascade (Windsurf-lineage) hooks
        info = payload.get("tool_info")
        info = info if isinstance(info, dict) else {}
        return _compact({
            "dialect": "cascade",
            "event": payload.get("agent_action_name"),
            "tool": info.get("mcp_tool_name"),
            "file": info.get("file_path"),
            "session": payload.get("trajectory_id"),
        })
    if "hook_event_name" in payload or "tool_name" in payload:  # Devin/Claude-format hooks
        tool_input = payload.get("tool_input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        return _compact({
            "dialect": "devin",
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "file": tool_input.get("file_path"),
            "session": payload.get("session_id"),
        })
    return {"dialect": "unknown", "keys": sorted(str(k) for k in payload)[:8]}


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` fields so log lines stay lean."""
    return {k: v for k, v in record.items() if v is not None}


def append(repo: Path, record: dict[str, Any]) -> None:
    """Append one record to the repo's firing log, creating the state dir if needed."""
    from cartogate.daemon.discovery import ensure_state_dir

    ensure_state_dir(repo)  # .cartogate exists and is self-ignoring
    path = repo / LOG_REL
    # Lock-free by design (unlike audit/ledger.py, which chains hashes and must lock):
    # each firing is a separate short-lived OS process doing one small O_APPEND write, and
    # a rare interleaved line only costs one diagnostic data point, never gate integrity.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _cmd_log(source: str) -> None:
    """Read the payload from stdin, classify it, and append the firing record."""
    try:
        # The read itself can raise (non-UTF-8 stdin) — the FIRING is still evidence, so
        # any failure on this path degrades to an "unparseable" record, never a lost line.
        payload: object = json.loads(sys.stdin.read())
    except (OSError, ValueError, UnicodeDecodeError):
        payload = None  # detect() -> "unparseable"
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        **detect(payload),
    }
    cwd = Path.cwd()
    append(find_repo_root(cwd) or cwd, record)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry (``cartogate-hook``). Exits 0 unconditionally — see module doc."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) >= 3 and args[0] == "log" and args[1] == "--source":
            _cmd_log(args[2])
        else:
            print("usage: cartogate-hook log --source <tag>  (reads payload on stdin)",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — a logging hook must never disturb the agent
        print(f"cartogate-hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
