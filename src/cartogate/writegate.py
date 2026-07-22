"""Write-time gate — in-loop hard enforcement as a first-class installed surface (F-13).

Reads an editor hook payload on stdin, extracts the source the agent proposes to write, and
checks each new symbol against the graph. On a duplicate it prints the full
BLOCKED/EVIDENCE/ACTION/anti-loop message to stderr and exits 2 — the exit code Claude Code,
Codex, and Windsurf's ``pre_write_code`` all treat as "block this action".

Payload shapes are auto-detected, so ONE command serves every harness:

- **Claude Code / Codex ``PreToolUse``**: the edit lives under ``tool_input``
  (``file_path`` + ``content``/``new_string``/…).
- **Windsurf ``pre_write_code``**: the edit is nested under ``tool_info`` (or, in older shapes,
  at the payload root) with tolerant code/path key naming; it is normalized to the Claude shape
  and gated by the identical logic.

The repo is auto-detected from the file being edited (nearest enclosing project root), so a
single hook config gates every repo; ``CARTOGATE_REPO`` pins a fixed graph instead. A warm
daemon answers instantly; without one the gate indexes in-process (resolution-free). Any
infrastructure error fails OPEN — the git pre-commit gate is the fail-closed backstop
(STRATEGY.md law 3: a slow or wedged hard gate trains the agent to route around it).

Installed as the ``cartogate-write-gate`` console script; ``cartogate init --agent
claude|windsurf`` wires it into the editor's hook config.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cartogate.daemon import client as daemon_client
from cartogate.extract.languages import language_of, symbol_facts_in
from cartogate.extract.pipeline import index_package
from cartogate.schema.enums import Language
from cartogate.store import InMemoryStore
from cartogate.surfaces import extract_proposed_text, gate_proposed_source, resolve_repo

EXIT_OK = 0
EXIT_BLOCK = 2  # Claude Code / Codex / Windsurf: exit 2 blocks the tool call.

#: Keys (within Windsurf's ``tool_info``, or at the payload root) that may carry the proposed
#: code — a superset of Windsurf's ``pre_write_code`` naming and Claude's Write/Edit shapes, so
#: the adapter tolerates payload-schema drift. An unmatched payload yields no text (fail open).
_WINDSURF_CODE_KEYS = (
    "content",
    "code",
    "code_edit",
    "new_code",
    "code_to_write",
    "text",
    "new_string",
    "new_str",
)
#: Keys that may carry the path of the file being written, in priority order.
_WINDSURF_PATH_KEYS = ("file_path", "path", "filepath", "file")


def file_path_of(payload: dict[str, object]) -> str | None:
    """The path of the file the payload proposes to write, if any (used to locate the repo)."""
    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    return file_path if isinstance(file_path, str) else None


def normalize(payload: dict[str, object]) -> dict[str, object]:
    """Map a Windsurf ``pre_write_code`` payload to the Claude-shaped ``{"tool_input": {...}}``.

    Windsurf nests the edit under ``tool_info``; other/older shapes may put the fields at the
    root. Both are handled. The returned ``tool_input`` uses the ``content``/``file_path`` keys
    that :func:`evaluate` already understands, so the downstream gate is shared verbatim.
    """
    info = payload.get("tool_info")
    source = info if isinstance(info, dict) else payload
    tool_input: dict[str, object] = {}
    for key in _WINDSURF_CODE_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            tool_input["content"] = value
            break
    for key in _WINDSURF_PATH_KEYS:
        value = source.get(key)
        if isinstance(value, str):
            tool_input["file_path"] = value
            break
    return {"tool_input": tool_input}


def evaluate(payload: dict[str, object], repo: Path, repo_id: str) -> list[dict[str, object]]:
    """Return the blocked verdicts for the source proposed in ``payload``.

    Prefers a running warm daemon (instant); falls back to an in-process resolution-free index
    when none is reachable.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    text = extract_proposed_text(tool_input)
    if not text.strip():
        return []

    # Gate in the language of the file being edited (default Python for unknown/absent paths).
    file_path = tool_input.get("file_path")
    language = (language_of(file_path) if isinstance(file_path, str) else None) or Language.PYTHON
    # The unit being edited, so re-declaring its own symbols is an edit, not a self-dup (F-28).
    editing_unit = _editing_unit(file_path, repo) if isinstance(file_path, str) else None

    try:
        blocked: list[dict[str, object]] = []
        for sym in symbol_facts_in(text, language):
            verdict = daemon_client.query(
                repo,
                "check_duplicate",
                {
                    "signature": sym.signature,
                    "language": language.value,
                    "exclude_unit": editing_unit,
                    # Old daemons ignore unknown keys (dispatch reads known args only), so
                    # version skew degrades to the old blocking behavior, never a false pass.
                    "proposed_body_hash": sym.body_hash,
                    "proposed_is_type_decl": sym.is_type_decl,
                },
            )
            if verdict.get("blocked"):
                blocked.append(verdict)
        return blocked
    except daemon_client.DaemonUnavailableError as exc:
        # Fall back to an in-process index. Stay quiet when no daemon was ever started (the
        # common case), but if one *should* be answering and isn't (a crash), say so — the gate
        # still works here, just slower, and the developer probably wants to restart it.
        if not exc.expected:
            print(
                f"Cartogate: warm daemon unreachable ({exc}); gating in-process (slower). "
                "Run `cartogate doctor` to diagnose.",
                file=sys.stderr,
            )

    store = InMemoryStore()
    index_package(repo, repo_id=repo_id, store=store, resolve=False)
    return gate_proposed_source(store, text, language, editing_unit=editing_unit)


def _editing_unit(file_path: str, repo: Path) -> str | None:
    """The store unit (POSIX rel path) for the edited file, matched against ``Node.unit``.

    Units are relative to the index base (``repo.parent``, the ``index_package`` default), so a
    symbol in the file being edited is recognized and not flagged as its own duplicate. Returns
    ``None`` for a path outside the repo (then the gate keeps its prior behavior).
    """
    try:
        return Path(file_path).resolve().relative_to(repo.parent).as_posix()
    except (ValueError, OSError):
        return None


def _record_write_blocks(
    payload: dict[str, object], blocked: list[dict[str, object]], repo: Path
) -> None:
    """Record each write-time BLOCK to the audit ledger (a ``write_block`` entry, no git tree).

    Best-effort — the audit write must NEVER downgrade a real block, so the WHOLE body is guarded
    (this call sits outside ``run``'s own fail-open try/except; an exception here would otherwise
    crash before the BLOCKED message + exit 2). Evidence uses the check_duplicate verdict's real
    keys (``existing_signature`` / ``existing_qualified_name``); language from the edited path.
    """
    try:
        from cartogate.stats import record_block

        fp = file_path_of(payload)
        language = language_of(fp) if isinstance(fp, str) else None
        lang_value = language.value if language is not None else "?"
        for verdict in blocked:
            record_block(
                repo, kind="write",
                signature=str(verdict.get("existing_signature") or ""),
                language=lang_value,
                existing=str(verdict.get("existing_qualified_name") or ""),
            )
    except Exception:  # noqa: BLE001 — never let audit recording break the block path.
        return


def run(payload: dict[str, object], *, env: dict[str, str], cwd: Path) -> int:
    """Resolve the repo from the payload, run the gate, and return the hook exit code.

    Shared by every harness adapter, so all enforce identically. Fails OPEN on any
    infrastructure error.
    """
    try:
        repo, repo_id = resolve_repo(file_path_of(payload), env=env, cwd=cwd)
        blocked = evaluate(payload, repo, repo_id)
    except Exception as exc:  # noqa: BLE001
        # Fail OPEN by design: an infrastructure error (un-indexable repo, missing deps, an
        # unrecognized language value) must not wedge the agent's edit loop. A real duplicate
        # still gets caught at the git pre-commit gate, which fails closed.
        print(f"Cartogate gate skipped ({exc}).", file=sys.stderr)
        return EXIT_OK

    if not blocked:
        return EXIT_OK
    _record_write_blocks(payload, blocked, repo)  # audit trail; best-effort, never affects exit
    for verdict in blocked:
        # The full BLOCKED/EVIDENCE/ACTION/anti-loop message: the shape measured to convert a
        # block into self-correction instead of an identical-retry loop.
        print(verdict.get("message") or f"Cartogate BLOCK: {verdict['reason']}", file=sys.stderr)
    return EXIT_BLOCK


def main() -> int:
    """Console-script / ``python -m`` entry: read the payload, auto-detect its shape, gate."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return EXIT_OK  # Not a payload we understand — do not block.
    if not isinstance(payload, dict):
        return EXIT_OK
    if not isinstance(payload.get("tool_input"), dict):
        # Windsurf's tool_info-nested payload, or a non-standard shape — normalize to the Claude
        # form (Claude's Write/Edit/MultiEdit always carry a dict tool_input and skip this).
        payload = normalize(payload)
    return run(payload, env=dict(os.environ), cwd=Path.cwd())


if __name__ == "__main__":
    raise SystemExit(main())
