"""Local stdio MCP server (spec §7.1) — the warm, deterministic gate surface.

A single long-lived process holds the graph **warm-resident** (the spec's cheapest path to
the p95 ≤ 50 ms gate, §8.5) and exposes the deterministic ``TOOL_SPECS`` tools over stdio — the
one transport confirmed available in locked-down environments (§9), needing a single whitelist
entry. The
server is a thin, data-driven adapter (``list_tools`` and ``call_tool`` both derive from
``TOOL_SPECS``/``dispatch``), so the surface can't drift from :mod:`cartogate.mcp.tools` and an
SDK API change (risk R6) is contained to this file, guarded by the list-tools smoke test.
"""

from __future__ import annotations

import contextlib
import faulthandler
import functools
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
import jsonschema
import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import cartogate
from cartogate.daemon import client as daemon_client
from cartogate.daemon import registry
from cartogate.daemon.discovery import (
    ensure_state_dir,
    is_pid_alive,
    read_discovery,
    remove_discovery,
)
from cartogate.daemon.refresh import GitLazyRefresh, RefreshStrategy
from cartogate.extract.pipeline import index_package
from cartogate.mcp.present import SURFACE_VERSION
from cartogate.mcp.tools import TOOL_SPECS, CartogateTools, dispatch
from cartogate.store import InMemoryStore
from cartogate.store.base import StoreInterface
from cartogate.surfaces import find_repo_root, looks_like_editor_install

SERVER_NAME = "cartogate"
_LOG = logging.getLogger("cartogate")

#: Cap on an incoming tool NAME. Client-controlled and uncapped by the SDK; no real tool name
#: comes close, so anything longer is junk input that must not reach an error message.
_MAX_TOOL_NAME_LEN = 512

#: A no-arg async function (``warm`` / ``refresh_once``).
_AsyncFn = Callable[[], Coroutine[Any, Any, None]]

#: How often the background task re-checks the working tree for edits. Refresh is fully async (never
#: blocks a tool call), so this only bounds how stale the served graph can be between rebuilds.
_REFRESH_POLL_S = 3.0

#: Forwarded-call timeout. Generous so a daemon doing an incremental refresh (F-36) doesn't trip it;
#: on a timeout/failure the call falls back to the in-process graph for the rest of the session.
_DAEMON_QUERY_TIMEOUT = 60.0

#: On the deferred path, back off this long after a failed workspace resolution before re-querying
#: the client — so an unresolvable session doesn't re-run roots/list on every single tool call.
_RESOLVE_RETRY_S = 30.0

#: How long an activation (set_workspace / first-call workspace_root) waits INLINE for the first
#: index before detaching it to the background. Snapshot-backed repos finish in seconds (one round
#: trip, result served immediately); a cold/large repo exceeds it and the call returns an explicit
#: "indexing, retry" payload instead — a tool call must NEVER out-wait the client's own timeout
#: (that's the hang-then-empty the editors show).
_ACTIVATE_WAIT_S = 15.0


def _no_daemon_requested(argv: list[str], env: dict[str, str]) -> bool:
    """True if the operator opted out of the daemon (``--no-daemon`` or ``CARTOGATE_NO_DAEMON``)."""
    if "--no-daemon" in argv:
        return True
    return env.get("CARTOGATE_NO_DAEMON", "").strip().lower() in ("1", "true", "yes", "on")


def _positional_repo(argv: list[str]) -> str | None:
    """The first non-flag argument — the repo path, if the config passed one (e.g. a client that
    expands ``${workspaceFolder}`` into args). ``None`` if only flags were given."""
    return next((a for a in argv if not a.startswith("-")), None)


def resolve_mcp_repo(
    argv: list[str], env: dict[str, str], cwd: Path
) -> tuple[Path, str] | None:
    """Which repo to index, or ``None`` if it can't be determined *safely*.

    Precedence: an explicit path argument, then ``CARTOGATE_REPO``, then the project root of
    ``cwd``. The cwd fallback is REFUSED when it lands on an editor/app install dir
    (:func:`looks_like_editor_install`) — a client that spawns the server without a workspace leaves
    cwd at its own install dir, and indexing that is never what the user wants. Returning ``None``
    lets the caller fail loudly with a fix, instead of silently indexing the wrong tree.
    """
    explicit = _positional_repo(argv) or env.get("CARTOGATE_REPO")
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        return repo, env.get("CARTOGATE_REPO_ID") or repo.name
    root = find_repo_root(cwd)
    if root is None or looks_like_editor_install(root):
        return None
    return root, env.get("CARTOGATE_REPO_ID") or root.name


#: Env vars an editor *might* set to the workspace path. None is guaranteed — the diagnostic log
#: below shows what a given client actually provides, so we can add the right one if needed.
_WORKSPACE_ENV_VARS = (
    "WORKSPACE_FOLDER",
    "WORKSPACE_ROOT",
    "PROJECT_ROOT",
    "VSCODE_WORKSPACE",
    "WINDSURF_WORKSPACE",
    "CODEIUM_WORKSPACE",
)


def _uri_to_path(uri: str) -> Path | None:
    """A ``file://`` root URI -> a local Path (handles the Windows ``file:///C:/…`` drive form)."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    try:
        return Path(url2pathname(parsed.path))
    except (OSError, ValueError):
        return None


def _valid_workspace(path: Path | None) -> Path | None:
    """A path that's a real directory and not an editor/app install dir, resolved; else ``None``."""
    if path is None:
        return None
    try:
        if path.is_dir() and not looks_like_editor_install(path):
            return path.resolve()
    except OSError:
        pass
    return None


#: Cap on the client roots/list round-trip. A client that declares roots but never answers must not
#: stall tool calls (the hang class we hardened git against) — time out and fall back.
_ROOTS_TIMEOUT_S = 5.0


async def _repo_from_client_roots(session: Any) -> Path | None:
    """The workspace the client (this editor window) reports via the MCP ``roots`` protocol.

    This is the per-window signal that makes multiple projects work: each window's server has its
    own session, so each reports its own workspace. Returns the first valid root, or ``None`` if the
    client doesn't support roots / reports none / doesn't answer within :data:`_ROOTS_TIMEOUT_S`.
    """
    try:
        with anyio.fail_after(_ROOTS_TIMEOUT_S):
            # Deliberate use of a DEPRECATED capability (SEP-2577) -- the raw request,
            # not the SDK's `session.list_roots()` courtesy wrapper. stdio handshakes
            # only ever negotiate the legacy protocol versions, where roots/list still
            # works and is the only ZERO-CONFIG per-window signal some editors give
            # (Windsurf). The 2026-07-28 era removes it with NO server-side replacement
            # -- there, context must come from the ladder's other rungs (workspace_root
            # / set_workspace / env / registry): the transport raises NoBackChannelError
            # and this rung degrades via the except below. The wrapper is skipped
            # because its @deprecated warning would fire per session for a decision made
            # on purpose,
            # and suppressing it needs warnings.catch_warnings -- process-global state
            # across an await, racy under the SDK's per-request task spawn (review
            # Medium 2026-08-19). Revisit when the SDK pin leaves 2.x (the entry guard
            # blocks 3.x) or a non-stdio transport lands.
            result = await session.send_request(
                types.ListRootsRequest(), types.ListRootsResult
            )
    except Exception as exc:  # noqa: BLE001 — no roots support, or a slow/hung client; degrade
        _LOG.info("cartogate-mcp: roots/list unavailable (%s)", exc)
        return None
    uris = [str(r.uri) for r in result.roots]
    _LOG.info("cartogate-mcp: client workspace roots = %s", uris)
    for uri in uris:
        repo = _valid_workspace(_uri_to_path(uri))
        if repo is not None:
            return repo
    return None


def _repo_from_workspace_env(env: dict[str, str]) -> Path | None:
    """A workspace path from one of the candidate editor env vars (best-effort)."""
    for var in _WORKSPACE_ENV_VARS:
        val = env.get(var)
        if not val:
            continue
        candidate = val.split(os.pathsep)[0]  # some are os.pathsep-joined lists
        repo = _valid_workspace(Path(candidate).expanduser())
        if repo is not None:
            _LOG.info("cartogate-mcp: workspace from env %s -> %s", var, repo)
            return repo
    return None


def _log_env_diagnostics(env: dict[str, str]) -> None:
    """Log the workspace-ish env the client passed us — the key clue when auto-detect fails."""
    hits = {
        k: v
        for k, v in env.items()
        if any(t in k.upper() for t in ("WORKSPACE", "FOLDER", "PROJECT", "VSCODE", "WINDSURF",
                                        "CODEIUM", "PWD"))
    }
    _LOG.info("cartogate-mcp: workspace-ish env seen = %s", hits or "{}")


def _log_client_diagnostics(session: Any) -> None:
    """Log which client connected + its declared capabilities — shows whether it supports roots."""
    params = getattr(session, "client_params", None)
    if params is not None:
        _LOG.info(
            "cartogate-mcp: client=%s capabilities=%s",
            getattr(params, "client_info", None), getattr(params, "capabilities", None),
        )


#: Resolve the workspace from a live client session -> (refresher, repo), or None if undeterminable.
_DeferredResolver = Callable[[Any], Coroutine[Any, Any, "tuple[RefreshStrategy, Path] | None"]]


def _make_deferred_resolver(env: dict[str, str], cwd: Path) -> _DeferredResolver:
    """Build the first-call resolver: MCP roots (per-window) -> workspace env -> guarded cwd ->
    the workspace registry (exactly one registered repo with a live resolved daemon)."""

    async def _resolve(session: Any) -> tuple[RefreshStrategy, Path] | None:
        _log_client_diagnostics(session)
        repo = await _repo_from_client_roots(session)
        if repo is None:
            repo = _repo_from_workspace_env(env)
        if repo is None:
            repo = _valid_workspace(find_repo_root(cwd))
        if repo is None:
            # The registry rung: sessions restart constantly and the editor gives no signal — but
            # a previously-activated repo whose daemon is STILL RUNNING is an unambiguous anchor
            # when it's the only one. (Two or more -> the agent must disambiguate; see the error.)
            live = await anyio.to_thread.run_sync(registry.live_daemon_workspaces)
            if len(live) == 1:
                repo = live[0]
                _LOG.info("cartogate-mcp: auto-connected to the only live daemon workspace: %s",
                          repo)
        if repo is None:
            return None
        repo_id = env.get("CARTOGATE_REPO_ID") or repo.name
        attach_repo_log_file(repo)  # route logs into the now-known project
        registry.register_workspace(repo)
        _LOG.info("cartogate-mcp: resolved workspace -> %s (id=%s)", repo, repo_id)
        refresh = GitLazyRefresh(repo, repo_id=repo_id, resolve=True, index_docs=True)
        return refresh, repo

    return _resolve


def _resolved_daemon_ready(repo: Path) -> bool:
    """True if a *resolved* daemon of the SAME VERSION AND RESULT SHAPE is running for ``repo``.

    Version-aware replacement is what makes package upgrades self-healing: a daemon still running
    OLDER code than this client (its files may even lock the venv against reinstall on Windows) is
    stopped, its discovery removed, and a fresh one spawned — the next call finds current code
    warm. Old daemons that predate the health ``version`` field report None and are replaced too.
    """
    info = read_discovery(repo)
    if info is None or not is_pid_alive(info.pid) or not info.resolve:
        return False  # no daemon, dead pid, or a structural-only daemon we can't use for all tools
    try:
        health = daemon_client.health(repo)
    except daemon_client.DaemonUnavailableError:
        return False
    # `surface` catches what the version cannot: an editable install's version does not change
    # when its source does, so a daemon started before a change to the tool results would have
    # its old-shape answers forwarded under this server's schemas. Daemons that predate the
    # field report None and are replaced too.
    if (
        health.get("version") != cartogate.__version__
        or health.get("surface") != SURFACE_VERSION
    ):
        _LOG.info(
            "cartogate-mcp: daemon for %s runs %s (result shape %s) but this client is %s "
            "(result shape %s) — replacing it",
            repo, health.get("version"), health.get("surface"),
            cartogate.__version__, SURFACE_VERSION,
        )
        _stop_daemon_process(info.pid, repo)
        _spawn_resolved_daemon(repo)  # fresh code warms in the background
        return False  # serve in-process this session; the next one lands on the new daemon
    return True


def _stop_daemon_process(pid: int, repo: Path) -> None:
    """Terminate a daemon and clear its discovery — quiet, never raises into serving.

    No pid-identity check needed HERE (unlike cli.py's stop paths): the caller just completed a
    tokened health round-trip — the same pid + port + 128-bit token all matched, so this pid IS
    our daemon with certainty. Don't copy this terminate into a context without that proof.
    """
    try:
        import psutil

        with contextlib.suppress(psutil.Error):
            psutil.Process(pid).terminate()
    except Exception as exc:  # noqa: BLE001 — replacement is best-effort
        _LOG.warning("cartogate-mcp: could not stop the stale daemon (%s)", exc)
    with contextlib.suppress(OSError):
        remove_discovery(repo)


def _spawn_resolved_daemon(repo: Path) -> None:
    """Fire-and-forget: start a detached resolved daemon so it's warm next session. Never blocks and
    never writes to *this* process's stdout (the daemon logs to ``.cartogate/daemon.log``)."""
    from cartogate.daemon.cli import daemon_python

    args = [
        daemon_python(), "-m", "cartogate.daemon.cli", "daemon", "start", str(repo),
        "--resolve", "--detach",
    ]
    try:
        subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
            # Windows: the intermediate `python -m ... daemon start --detach` is a console app; a
            # console-less parent (this MCP server) would otherwise flash a terminal window for it.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        _LOG.warning("could not auto-start the resolved daemon (%s); serving in-process", exc)


def _repo_log_formatter() -> logging.Formatter:
    return logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def attach_repo_log_file(repo: Path) -> None:
    """After a *deferred* resolution, also route logs to the resolved repo's ``.cartogate/mcp.log``
    (startup logged to a fallback because the repo wasn't known yet). No-op if a file override set.
    """
    if os.environ.get("CARTOGATE_LOG_FILE"):
        return
    try:
        log_file = ensure_state_dir(repo) / "mcp.log"
        root = logging.getLogger()
        # Idempotent by path: set_workspace can be called repeatedly (an agent re-pointing at the
        # same root, a retargeting first call), and without this each one leaked another open
        # FileHandler — a fd per call, and every later log line written once PER handler.
        #
        # Both sides are normalized the SAME way, which is load-bearing on Windows: FileHandler
        # stores os.path.abspath(), which does NOT agree with Path.resolve() for 8.3 short names,
        # drive-letter case, or junctions — comparing raw would silently no-op the dedup for any
        # caller that hasn't already resolved its path.
        target = os.path.normcase(str(log_file.resolve()))
        if any(
            isinstance(h, logging.FileHandler)
            and os.path.normcase(str(Path(h.baseFilename).resolve())) == target
            for h in root.handlers
        ):
            return
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(_repo_log_formatter())
        root.addHandler(handler)
        _LOG.info("cartogate logs now also -> %s", log_file)
    except OSError as exc:
        _LOG.warning("cartogate: could not attach the repo log file (%s)", exc)


def configure_logging(repo: Path | None) -> None:
    """Route cartogate logs to stderr (which the MCP client captures) *and* a file you can ``tail``.

    Knobs via the environment: ``CARTOGATE_LOG_LEVEL`` (default ``INFO``; ``NOTSET`` logs all,
    ``WARNING`` quiets the server); ``CARTOGATE_LOG_FILE`` to override the path (default
    ``<repo>/.cartogate/mcp.log``), or ``none`` to disable the file. When ``repo`` is ``None`` (a
    deferred start — the workspace isn't known until the client reports it), the file falls back to
    ``~/.cartogate/mcp.log`` and :func:`attach_repo_log_file` adds the repo's log once resolved. A
    stdio server must keep *stdout* clean for the protocol, so logs go to stderr + file only.
    """
    level = getattr(logging, os.environ.get("CARTOGATE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    setting = os.environ.get("CARTOGATE_LOG_FILE")
    if setting is None:
        log_file: Path | None = ensure_state_dir(repo or Path.home()) / "mcp.log"
    elif setting.strip().lower() in ("", "none", "off"):
        log_file = None
    else:
        log_file = Path(setting)
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:  # a read-only/odd path must not stop the server from starting
            sys.stderr.write(
                f"cartogate: could not open log file {log_file} ({exc}); logging to stderr only\n"
            )
            log_file = None  # so the success log below doesn't claim a file that isn't attached

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    if log_file is not None:
        _LOG.info("cartogate logs -> stderr + %s (level %s)", log_file, logging.getLevelName(level))


#: Keeps the faulthandler crash-log handle alive for the process lifetime (faulthandler writes to
#: a raw fd at crash time — the file must stay open).
_CRASH_LOG_FH: Any = None


def enable_crash_visibility(log_file: Path | None) -> bool:
    """Make every way this process can die leave a trace ("transport closed" must be explainable).

    ``faulthandler`` dumps all thread stacks to ``<log dir>/crash.log`` on a NATIVE fault (a
    segfault in tree-sitter/jedi C extensions, a fatal interpreter error) — the one death that
    Python logging can never record. Unhandled *Python* exceptions are covered separately by
    :func:`main`'s catch-log-reraise. Returns True when the file-backed handler is active.
    """
    global _CRASH_LOG_FH
    if log_file is None:
        faulthandler.enable()  # stderr only — better than nothing
        return False
    try:
        crash_log = log_file.parent / "crash.log"
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG_FH = open(crash_log, "a", encoding="utf-8")  # noqa: SIM115 — lifetime = process
        faulthandler.enable(file=_CRASH_LOG_FH)
        return True
    except OSError:
        faulthandler.enable()
        return False


def _rss_mb() -> int | None:
    """This process's resident memory in MB (None if psutil can't say) — logged at index commits so
    an out-of-memory death is visible as a climbing series in the log before the end."""
    try:
        import psutil

        return int(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:  # noqa: BLE001 — diagnostics must never break serving
        return None


SET_WORKSPACE_TOOL = "set_workspace"

#: Behavioural hints for the QUERY tools: they answer from the extracted graph and never run
#: user code, so an agent can call them freely — nothing to undo, nothing to confirm, no
#: repeat-call penalty. That is the point of the surface: a gate the agent avoids because it
#: looks expensive or risky is a gate that doesn't fire (STRATEGY.md law 3). ``open_world`` is
#: False because every answer comes from the local graph — no network, no external service.
#:
#: Deliberate call on ``read_only``: on the lazy server a first call may also write Cartogate's
#: OWN bookkeeping — the graph snapshot, mcp.log, and the workspace registry — all inside the
#: self-ignoring ``.cartogate/`` dir or ``CARTOGATE_HOME``, never the user's source. That is
#: cache/telemetry maintenance, the same class as any caching read, so it stays read-only.
#: Marking all of them non-read-only to cover it would destroy the "call me freely" property
#: this whole annotation exists to create.
_QUERY_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

#: ``contract_status`` is NOT a query: answering honestly means RUNNING the contract's declared
#: ``run:`` checks, which are arbitrary user-supplied shell commands (``verify.evaluate`` ->
#: ``subprocess.run(..., shell=True)``), plus a ``git write-tree`` that writes objects. It gets
#: its own hints because hosts use ``read_only`` to AUTO-APPROVE calls without prompting the
#: user, and auto-approving "run whatever this repo declares" is not a promise we can make.
#: destructive: a check can be `make clean`. not idempotent: re-running has further effects.
#: open_world: a check can reach the network.
_CONTRACT_STATUS_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)

#: ``set_workspace`` CHANGES server state (retargets the workspace and triggers an index), so it
#: is not read-only. It is non-destructive (nothing is deleted) and idempotent — the same root
#: twice lands in the same state, which :func:`attach_repo_log_file` upholds by refusing to
#: attach a second handler for a path it is already logging to.
_SET_WORKSPACE_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

#: Maps a spec's declared ``safety`` class to its MCP hints. Looked up WITHOUT a default, on
#: purpose: a tool whose spec omits ``safety`` (or names a class that isn't here) raises KeyError
#: at :func:`_tool_definitions` instead of silently inheriting the permissive read-only hints.
#: That polarity matters — the safe-by-default version of this dict is how the ``contract_status``
#: mislabel happened, and hosts auto-approve read-only calls without asking the user. Adding a
#: tool must force this decision, the same way the required ``title`` key already does.
_ANNOTATIONS_BY_SAFETY = {
    "query": _QUERY_ANNOTATIONS,
    "runs_user_code": _CONTRACT_STATUS_ANNOTATIONS,
}

#: The lazy server can answer a SUCCESSFUL call with a payload that is not the tool's dispatch
#: result: "which workspace?" guidance, a `set_workspace` activation, first-index progress. Once a
#: tool advertises an ``output_schema`` those payloads are validated against it too — the SDK
#: client REJECTS any success result whose structured content doesn't conform — so every lazy
#: tool's advertised schema is the union of its dispatch shape and this one. The anyOf gives the
#: branch teeth (a session payload always carries ``ok`` or ``error``) without constraining the
#: dispatch branch.
_OPERATIONAL_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "A session payload instead of the tool's result: workspace-resolution guidance, a "
        "set_workspace activation, or first-index progress. Follow `note`/`error`, then retry."
    ),
    "properties": {
        "ok": {"type": "boolean"},
        "status": {"type": "string", "description": "'indexing' while a first build runs."},
        "repo": {"type": "string"},
        "repo_id": {"type": "string"},
        "nodes": {"type": "integer"},
        "mode": {"type": "string", "description": "'daemon' or 'inproc'."},
        "elapsed_s": {"type": "number"},
        "note": {"type": "string"},
        "error": {"type": "string"},
        "action": {"type": "string", "description": "The tool to call to fix this."},
        "known_workspaces": {"type": "array", "items": {"type": "string"}},
    },
    "anyOf": [{"required": ["ok"]}, {"required": ["error"]}],
}


#: The same session payload as the branch of every lazy tool's union: only the part with teeth
#: (it carries ``ok`` or ``error``). The fully typed and described form above is advertised once,
#: by ``set_workspace``; repeating its 782 bytes in seventeen unions cost 13 KB of the listing.
#: The trade: a validating client no longer type-checks a session payload's fields here, and a
#: tool result that wrongly carried a top-level ``ok`` or ``error`` would pass as one. No tool
#: result has either key (the daemon's own ok/error envelope is unwrapped before forwarding).
_OPERATIONAL_BRANCH: dict[str, Any] = {
    "type": "object",
    "anyOf": _OPERATIONAL_RESULT_SCHEMA["anyOf"],
}


def _with_operational_shapes(schema: dict[str, Any]) -> dict[str, Any]:
    """A lazy-server output schema: the tool's own shape OR an operational session payload."""
    return {"type": "object", "anyOf": [schema, _OPERATIONAL_BRANCH]}


#: A server-control tool (only the lazy MCP server advertises it): the agent tells Cartogate which
#: project it's in — the per-window signal an editor that gives no roots/workspace-env can't.
_SET_WORKSPACE_DEF = types.Tool(
    name=SET_WORKSPACE_TOOL,
    title="Set workspace",
    annotations=_SET_WORKSPACE_ANNOTATIONS,
    output_schema=_OPERATIONAL_RESULT_SCHEMA,
    description=(
        "Point Cartogate at the project you're working in. Call this ONCE with the absolute path "
        "the workspace/repository root when a Cartogate tool reports it couldn't determine the "
        "workspace. Cartogate then indexes that project and every other tool works for the session."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "Absolute path to the project/repo root."}
        },
        "required": ["root"],
    },
)


#: Optional self-configuration parameter injected into every lazy-server tool: the agent can pass
#: its workspace root ON the first tool call — one deterministic round trip, no failed call first.
WORKSPACE_ROOT_PARAM = "workspace_root"

#: Kept to one line on purpose: this schema is copied into EVERY lazy tool, so each word here is
#: paid seventeen times in the tool listing. The server instructions carry the full explanation
#: (why to pass it, and that set_workspace switches projects) once. It does not say "ignored
#: afterwards": after an auto-resolved workspace a different root IS honoured.
_WORKSPACE_ROOT_SCHEMA = {
    "type": "string",
    "description": "Absolute path of the workspace root. Pass it on your first call.",
}


def _with_workspace_root(schema: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of a tool input schema with the optional ``workspace_root`` param added
    (never mutates TOOL_SPECS — the static server and daemon serve the original schemas)."""
    out: dict[str, Any] = json.loads(json.dumps(schema))
    props = out.setdefault("properties", {})
    # A future tool defining its own `workspace_root` would be silently clobbered here — fail loud.
    assert WORKSPACE_ROOT_PARAM not in props, f"tool schema already defines {WORKSPACE_ROOT_PARAM}"
    props[WORKSPACE_ROOT_PARAM] = dict(_WORKSPACE_ROOT_SCHEMA)
    return out


def _tool_definitions(*, with_set_workspace: bool = False) -> list[types.Tool]:
    """Translate the transport-independent TOOL_SPECS into MCP Tool objects.

    ``with_set_workspace`` (the lazy server) appends the control tool AND injects the optional
    ``workspace_root`` param into every tool, so the agent's first call can self-configure the
    workspace deterministically. The static server serves the specs untouched.
    """
    tools = [
        types.Tool(
            name=spec["name"],
            title=spec["title"],
            description=spec["description"],
            annotations=_ANNOTATIONS_BY_SAFETY[spec["safety"]],
            input_schema=_with_workspace_root(spec["input_schema"])
            if with_set_workspace
            else spec["input_schema"],
            # The lazy server can answer with an operational payload instead of the dispatch
            # result, so its advertised schema must admit both; the static server only ever
            # serves dispatch results and advertises the exact shape.
            output_schema=_with_operational_shapes(spec["output_schema"])
            if with_set_workspace
            else spec["output_schema"],
        )
        for spec in TOOL_SPECS
    ]
    if with_set_workspace:
        tools.append(_SET_WORKSPACE_DEF)
    return tools


def _new_server(*, instructions: str | None = None) -> Server:
    """A ``Server`` carrying Cartogate's identity.

    ``version`` matters: it reaches the client in the initialize handshake, which is the one
    place an agent (or a bug report) can see WHICH build answered. The SDK defaults it to an
    empty string, which would silently drop the "an installed build always identifies its exact
    commit" guarantee that ``cartogate --version`` and the mcp.log banner otherwise uphold.

    No ``cache_hints`` here, and that is deliberate rather than an oversight: the SDK only
    serializes ``ttlMs``/``cacheScope`` on protocol ``2026-07-28``, which is the stateless
    per-request-envelope era reached via ``server/discover``. Cartogate serves stdio, whose only
    entry is the ``initialize`` handshake, so that version can never be negotiated here and any
    hint we set would be silently dropped on the wire. Revisit if a non-stdio transport lands.
    (``tools/call`` would be ineligible regardless — the SDK's CacheableMethod set excludes it,
    and tool results track the working tree, so they must never be cached.)
    """
    return Server(
        SERVER_NAME,
        version=cartogate.__version__,
        title="Cartogate",
        instructions=instructions,
    )


def _register_static_handlers(
    server: Server,
    *,
    with_set_workspace: bool = False,
    status: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Register the graph-independent handlers: the static tool list, a STATUS resource, and empty
    prompts. Cartogate is tools-first, but clients also touch the resource surface: Windsurf probes
    ``resources/list`` at connect, and an ``@cartogate`` mention in Cascade issues a
    ``resources/read`` for ``mcp://cartogate`` — without a read handler that surfaced as
    "Method not found" to the user. Any read now returns the live status JSON (workspace, mode,
    tools), which doubles as useful orientation for the agent. None of these touch the warm graph,
    so they stay instant."""

    async def _list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams,
    ) -> types.ListToolsResult:
        tools = _tool_definitions(with_set_workspace=with_set_workspace)
        return types.ListToolsResult(tools=tools)

    async def _list_resources(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams,
    ) -> types.ListResourcesResult:
        resources = [
            types.Resource(
                uri="cartogate://status",
                name="Cartogate status",
                description="Current workspace, serving mode, and the available tools.",
                mime_type="application/json",
            )
        ]
        return types.ListResourcesResult(resources=resources)

    async def _read_resource(
        ctx: ServerRequestContext[Any, Any], params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        # Served for ANY requested URI: clients ask for their own spellings (Windsurf's @mention
        # reads mcp://cartogate) and an error here reads as a broken server to the user.
        payload: dict[str, Any] = status() if status is not None else {"server": SERVER_NAME}
        payload.setdefault("tools", [str(spec["name"]) for spec in TOOL_SPECS])
        text = json.dumps(payload, indent=2)
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=params.uri, mime_type="application/json", text=text,
                )
            ]
        )

    async def _list_prompts(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams,
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[])

    server.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools)
    server.add_request_handler("resources/list", types.PaginatedRequestParams, _list_resources)
    server.add_request_handler("resources/read", types.ReadResourceRequestParams, _read_resource)
    server.add_request_handler("prompts/list", types.PaginatedRequestParams, _list_prompts)


def build_server(store: StoreInterface, *, refresh: RefreshStrategy | None = None) -> Server:
    """Wire a :class:`Server` whose tool handlers query the warm graph.

    With ``refresh`` set, the server checks for working-tree changes before each tool call and
    swaps in a freshly-built graph when files changed — so ``blast_radius`` / ``find_references`` /
    ``suggest_tests`` / … reflect the *current* code, not the snapshot at startup. The check is
    debounced and runs in a worker thread, so an unchanged tree costs almost nothing. Without
    ``refresh`` the store is static (used by tests).
    """
    state = {"tools": CartogateTools(store)}
    server: Server = _new_server()
    _register_static_handlers(server)
    validate_arguments = _argument_validator()

    async def _call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        rejected = validate_arguments(params.name, dict(params.arguments or {}))
        if rejected is not None:
            return rejected
        if refresh is not None:
            new_store = await anyio.to_thread.run_sync(refresh.maybe_refresh)
            if new_store is not None:  # the tree changed — serve the rebuilt graph from now on
                state["tools"] = CartogateTools(new_store)
        tools = state["tools"]
        # Run the (possibly heavy: find_cycles, blast_radius on a big graph) query in a worker
        # thread so it never blocks the event loop — the server stays responsive to other calls.
        result = await anyio.to_thread.run_sync(
            dispatch, tools, params.name, params.arguments or {}
        )
        return _json_result(result)

    server.add_request_handler(
        "tools/call", types.CallToolRequestParams, _guard_tool_errors(_call_tool)
    )
    return server


def load_warm_store(repo: Path, *, repo_id: str) -> InMemoryStore:
    """Index ``repo`` once into an in-memory store kept warm for the server's lifetime."""
    store = InMemoryStore()
    index_package(repo, repo_id=repo_id, store=store)
    return store


def _text_result(obj: object) -> list[types.ContentBlock]:
    """A tool result carrying a JSON object as text (the MCP tool-result shape)."""
    # Separator-free: a host that forwards this text block to its model pays for every character,
    # and json.dumps' default ", " / ": " are about 4% of a typical result.
    return [types.TextContent(type="text", text=json.dumps(obj, separators=(",", ":")))]


def _json_result(payload: dict[str, Any]) -> types.CallToolResult:
    """A SUCCESSFUL tool result, dual-emitted: the JSON text block every client understands plus
    ``structured_content`` for schema-aware 2.x clients — the same payload in both channels, so a
    structured client and a text-only client never see different answers.

    Every success path must come through here once a tool advertises an ``output_schema``: the
    SDK client rejects a success result whose structured content is absent. Error results
    (``is_error=True``) stay on :func:`_text_result` — the spec exempts them from the schema.
    """
    return types.CallToolResult(content=_text_result(payload), structured_content=payload)


def _argument_validator(
    *, with_set_workspace: bool = False
) -> Callable[[str, dict[str, Any]], types.CallToolResult | None]:
    """Build this server's ``(name, arguments) -> error result | None`` schema check.

    mcp 1.x validated every ``tools/call`` against the tool's advertised ``inputSchema``
    before the handler ran. 2.0 dropped per-tool validation — only the JSON-RPC envelope is
    model-validated — so without this an ill-typed argument reaches ``dispatch``, gets
    discarded by a downstream isinstance guard, and the call returns *success*: a silently
    served bad call where there used to be a rejection. Validating here restores the 1.x
    contract at the one place every tool call passes through.

    The schema index is built once per server from :func:`_tool_definitions`, so the check
    can't drift from what ``tools/list`` advertises.
    """
    # Compiled once, not per call: `jsonschema.validate()` re-runs check_schema and rebuilds the
    # validator every time (~0.4-1.3 ms/call against the 50 ms p95 budget). Compiling here also
    # moves a malformed schema's SchemaError to server build, where it's a startup failure we
    # control, instead of an uncaught exception on some later tool call.
    validators: dict[str, Any] = {}
    for tool in _tool_definitions(with_set_workspace=with_set_workspace):
        schema = tool.input_schema
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validators[tool.name] = validator_cls(schema)

    def _check(name: str, arguments: dict[str, Any]) -> types.CallToolResult | None:
        validator = validators.get(name)
        if validator is None:
            return None  # unknown tool — dispatch owns that error path, unchanged
        try:
            validator.validate(arguments)
        except jsonschema.ValidationError as exc:
            msg = f"invalid arguments for '{name}': {exc.message}"
            # Truncated in the log only: jsonschema embeds the offending VALUE in its message,
            # so a megabyte-sized bad argument would otherwise land whole in the unrotated
            # mcp.log. The client still gets the full message, as it did under 1.x.
            _LOG.warning("cartogate-mcp: %s", msg[:500])
            return types.CallToolResult(content=_text_result({"error": msg}), is_error=True)
        return None

    return _check


#: A ``tools/call`` handler: ``(ctx, params) -> CallToolResult``.
_ToolHandler = Callable[
    [ServerRequestContext[Any, Any], types.CallToolRequestParams],
    Coroutine[Any, Any, types.CallToolResult],
]


def _guard_tool_errors(handler: _ToolHandler) -> _ToolHandler:
    """Turn an escaping handler exception into an ``is_error`` result, as mcp 1.x did.

    The 1.x ``@server.call_tool()`` decorator wrapped every handler in
    ``except Exception -> CallToolResult(isError=True)``. 2.0's ``add_request_handler`` has no
    such net, so an exception becomes a JSON-RPC *protocol* error instead — which reads to the
    agent as a broken server rather than a failed tool, and diverges from the daemon surface,
    which returns a structured error for the same conditions (``dispatch``'s "unknown tool",
    a tool method raising, a non-serializable result). Restores the 1.x contract.
    """

    @functools.wraps(handler)
    async def _guarded(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if len(params.name) > _MAX_TOOL_NAME_LEN:
            # Bounded at the SOURCE, not just where it's logged: the name is client-controlled and
            # uncapped by the SDK, and `dispatch` embeds it in its "unknown tool" error — which
            # `_LOG.exception` then renders IN THE TRACEBACK, where a downstream truncation can't
            # reach. mcp.log is unrotated and lives in the user's repo, so an agent looping on
            # junk calls would otherwise fill the disk. No real tool name approaches this.
            _LOG.warning("cartogate-mcp: rejected a %d-char tool name", len(params.name))
            return types.CallToolResult(
                content=_text_result({"error": "tool name too long"}), is_error=True
            )
        try:
            return await handler(ctx, params)
        except Exception as exc:  # noqa: BLE001 — parity with the 1.x decorator's catch-all
            # Both truncated: the tool NAME is client-controlled and uncapped by the SDK, and it
            # reaches the log twice (the format arg, then again inside the traceback's exception
            # line). mcp.log is unrotated and lives in the user's repo, so an agent looping on
            # bad calls would otherwise be a disk-fill vector.
            _LOG.exception("cartogate-mcp: tool %r failed", params.name[:200])
            return types.CallToolResult(
                content=_text_result({"error": str(exc)[:2000]}), is_error=True
            )

    return _guarded


async def _bounded_prime(refresh: Any) -> tuple[dict[str, Any], float]:
    """Prime ``refresh`` in a worker thread, waiting inline at most :data:`_ACTIVATE_WAIT_S`.

    Returns ``(box, t0)``. ``box`` carries ``store`` on inline success or ``error`` on an inline
    failure; an EMPTY box means the bound elapsed and the thread was ABANDONED — it keeps building
    and drops its result into the same box, which the caller records for later commit (the
    ``state["indexing"]`` machinery). ``box`` needs no lock: the thread writes exactly one str key
    once, as its last action, and dict item assignment of a fully-constructed object is atomic
    under the GIL; readers only check key presence.
    """
    box: dict[str, Any] = {}

    def _build() -> None:
        try:
            box["store"] = refresh.prime()
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean error, not a fault
            box["error"] = str(exc)

    t0 = time.monotonic()
    with anyio.move_on_after(_ACTIVATE_WAIT_S):
        await anyio.to_thread.run_sync(_build, abandon_on_cancel=True)
    return box, t0


def build_lazy_server(
    refresh: RefreshStrategy | None,
    repo: Path | None,
    *,
    daemon_ready: bool = False,
    resolver: _DeferredResolver | None = None,
) -> tuple[Server, _AsyncFn, _AsyncFn]:
    """A server that builds its warm graph lazily, returned with ``warm()`` and ``refresh_once()``.

    Three properties make this usable on a large repo:

    * **The handshake never blocks on the index.** ``list_tools`` needs no graph (the tool set is
      static); ``warm()`` builds the graph on first need under a lock (so a background pre-warm and
      a racing first call can't double-index). The caller pre-warms during the idle window after
      connect, so the first real call is usually instant.
    * **A tool call never blocks on a *re*build.** A resolved graph can't refresh incrementally, so
      every edit forces a full re-index (minutes). Doing that inside the tool call (the old
      behavior) meant the first call after any edit hung for the whole rebuild. Instead
      ``call_tool`` always serves the *current* graph immediately, and ``refresh_once()`` (driven by
      a background loop) rebuilds out of band and swaps the graph in when ready.
    * **Shares one warm graph when a daemon is up.** With ``daemon_ready`` set, ``call_tool``
      forwards to the warm resolved daemon (no in-process graph built); if the daemon goes away mid-
      session it falls back to building its own graph for the rest of the session.
    """
    # ``refresh``/``repo`` may be None (deferred): the workspace is resolved from the client's roots
    # on the first tool call, via ``resolver``. The eager (pinned) path leaves them set as before.
    # ``indexing`` is a detached first-build in flight: {"root", "repo_id", "refresh", "box", "t0"}
    # — see ``_activate``. Tool calls report progress instead of blocking on it.
    # ``resolved_via``: how the current workspace was chosen — "pinned" (arg/env at startup),
    # "agent" (set_workspace / workspace_root), or "auto" (roots / registry auto-connect). An
    # AUTO-derived workspace may be re-targeted by a later workspace_root (the agent knows its
    # window better than our heuristics); agent/pinned choices stay sticky.
    state: dict[str, Any] = {
        "tools": None, "refresh": refresh, "repo": repo, "indexing": None,
        "resolved_via": "pinned" if refresh is not None else None,
    }
    daemon_on = [daemon_ready]  # mutable cell so call_tool can flip to in-process on daemon loss
    lock = anyio.Lock()

    async def warm(session: Any = None) -> None:
        # Fast-path: once primed, serve immediately WITHOUT taking the lock — a tool call must never
        # wait on the background refresh (which holds the lock while it re-scans/rebuilds). Reading
        # the already-set reference is atomic; refresh_once only ever swaps in a newer graph.
        if state["tools"] is not None:
            return
        async with lock:
            if state["tools"] is not None:
                return
            if state["indexing"] is not None:
                # A detached first build is already in flight (a concurrent call could be past its
                # early poll when it was recorded) — never start a second; the caller's post-warm
                # poll reports/commits it.
                return
            if state["refresh"] is None:  # deferred: resolve the workspace from the client now
                if session is None or resolver is None:
                    return  # a background pre-warm with no session yet — wait for a real tool call
                if time.monotonic() < state.get("resolve_after", 0.0):
                    return  # recently failed — don't re-query the client on every call
                resolved = await resolver(session)
                if resolved is None:
                    # workspace undeterminable — back off; _call_tool then returns a clear error
                    state["resolve_after"] = time.monotonic() + _RESOLVE_RETRY_S
                    return
                state["refresh"], state["repo"] = resolved
                state["resolved_via"] = "auto"  # roots/registry — re-targetable by the agent
                if await anyio.to_thread.run_sync(_resolved_daemon_ready, state["repo"]):
                    # The resolved workspace (roots or registry auto-connect) has a live daemon —
                    # forward to it instead of building an in-process graph.
                    daemon_on[0] = True
                    _LOG.info("cartogate-mcp: using the warm daemon for %s", state["repo"])
                    return
            _LOG.info("indexing the workspace (resolved, doc-aware); first query waits briefly")
            # Bounded like _activate — this path ALSO builds first indexes (the eager first call,
            # the roots-resolved first call, and the daemon-loss fallback where set_workspace
            # stored an unprimed refresher). An unbounded inline prime here out-waited the client's
            # timeout on big repos (the hang-then-empty class #99 fixed for _activate).
            box, t0 = await _bounded_prime(state["refresh"])
            if "store" in box:
                state["tools"] = CartogateTools(box["store"], root=state["repo"])
                _LOG.info(
                    "index ready: %d nodes in %.1fs (rss %s MB)",
                    len(box["store"].visible_node_ids()),
                    time.monotonic() - t0,
                    _rss_mb(),
                )
                return
            # Still building (or failed late) — hand it to the indexing record; _call_tool's poll
            # reports progress / the failure instead of the misleading "unknown workspace" error.
            state["indexing"] = {
                "root": state["repo"], "repo_id": getattr(state["repo"], "name", "?"),
                "refresh": state["refresh"], "box": box, "t0": t0,
                "via": state.get("resolved_via") or "auto",
            }
            _LOG.info("cartogate-mcp: first index continues in the background")

    async def refresh_once() -> None:
        """Background only: rebuild + swap if the tree changed. Never on the tool-call path.

        Shares ``warm()``'s lock, so ``maybe_refresh`` can never run concurrently with the initial
        ``prime`` or with another ``refresh_once`` (both would race the refresher's internal state).
        """
        if state["tools"] is None:
            return  # not primed yet — warm() owns the first build
        async with lock:
            t0 = time.monotonic()
            new_store = await anyio.to_thread.run_sync(state["refresh"].maybe_refresh)
            if new_store is not None:
                state["tools"] = CartogateTools(new_store, root=state["repo"])
                # state["refresh"], not the outer `refresh` (None in a deferred session).
                info = getattr(state["refresh"], "last_refresh", None)
                _LOG.info(
                    "graph refreshed (%s, %s files) in %.1fs",
                    getattr(info, "mode", "?"),
                    getattr(info, "reindexed", "?"),
                    time.monotonic() - t0,
                )

    # Server instructions (surfaced to the agent at connect): when the workspace isn't pinned, tell
    # it to set_workspace UP FRONT — so it starts cleanly instead of a failed call, then retry.
    instructions = (
        "Cartogate answers questions about the code in the project you're working in. This editor "
        "doesn't tell the server which project is open, so on your FIRST Cartogate call of the "
        f"session, include the `{WORKSPACE_ROOT_PARAM}` parameter (every tool accepts it) set to "
        "the absolute path of the workspace/repo folder open in your editor — the call configures "
        "the workspace and runs in one step. Alternatively call `set_workspace` once. Every tool "
        "then operates on that project."
    ) if refresh is None else None
    server: Server = _new_server(instructions=instructions)

    def _status() -> dict[str, Any]:
        """Live orientation for the resource surface (an @cartogate mention reads this)."""
        if daemon_on[0]:
            mode = "daemon"
        elif state["tools"] is not None:
            mode = "in-process"
        elif state["indexing"] is not None:
            mode = "indexing"
        else:
            mode = "awaiting workspace (pass workspace_root on a tool call, or call set_workspace)"
        return {
            "server": SERVER_NAME,
            "version": cartogate.__version__,
            "workspace": str(state["repo"]) if state["repo"] is not None else None,
            "mode": mode,
        }

    _register_static_handlers(server, with_set_workspace=True, status=_status)
    validate_arguments = _argument_validator(with_set_workspace=True)

    async def _activate(root_arg: object) -> dict[str, object]:
        """Point the session at a workspace root; the shared core of ``set_workspace`` and the
        per-tool ``workspace_root`` param. Returns the result payload (``ok`` + details).

        No ``looks_like_editor_install`` guard here (unlike auto-detect): the agent named this path
        explicitly, so we trust its intent. On failure we return ``{ok: false}`` and leave any prior
        workspace intact (the state writes below only run after a successful ``prime``).
        """
        if not isinstance(root_arg, str) or not root_arg.strip():
            return {"ok": False, "error": "set_workspace needs a 'root' path string"}
        root = Path(root_arg).expanduser().resolve()
        if not root.is_dir():
            return {"ok": False, "error": f"not a directory: {root}"}
        repo_id = os.environ.get("CARTOGATE_REPO_ID") or root.name
        registry.register_workspace(root)  # remember across sessions (the auto-connect anchor)
        async with lock:
            attach_repo_log_file(root)
            pending = state["indexing"]
            if pending is not None and pending["root"] == root:
                # This root's first build is already in flight — report/commit it, don't duplicate
                # (checked before constructing another refresher: exactly one build per root).
                if "store" in pending["box"]:
                    state["refresh"], state["repo"] = pending["refresh"], root
                    state["tools"] = CartogateTools(pending["box"]["store"], root=root)
                    state["resolve_after"], daemon_on[0] = 0.0, False
                    state["indexing"] = None
                    state["resolved_via"] = "agent"
                    nodes = len(pending["box"]["store"].visible_node_ids())
                    return {"ok": True, "repo": str(root), "repo_id": repo_id, "nodes": nodes,
                            "mode": "inproc"}
                if "error" in pending["box"]:
                    state["indexing"] = None
                    return {"ok": False,
                            "error": f"could not index {root}: {pending['box']['error']}"}
                state["resolved_via"] = "agent"  # the agent claimed this root; stop re-firing
                return {"ok": True, "repo": str(root), "repo_id": repo_id, "status": "indexing",
                        "note": "first index of this repo is still building — retry your call "
                        "in ~30 seconds"}
            # A resolved refresher for this root — the daemon-loss fallback OR the in-process graph.
            # Overwrites any prior (auto-resolved / previously-set) workspace.
            new_refresh = GitLazyRefresh(root, repo_id=repo_id, resolve=True, index_docs=True)
            if await anyio.to_thread.run_sync(_resolved_daemon_ready, root):
                # `cartogate init` left a warm resolved daemon for this repo — forward to it (shared
                # graph, no in-process index). We keep the refresher so a daemon-loss falls back.
                state["refresh"], state["repo"], state["tools"] = new_refresh, root, None
                state["resolve_after"], daemon_on[0] = 0.0, True
                state["indexing"] = None  # the daemon supersedes any detached first build
                state["resolved_via"] = "agent"
                _LOG.info("cartogate-mcp: set_workspace -> %s (using the warm daemon)", root)
                return {"ok": True, "repo": str(root), "repo_id": repo_id, "mode": "daemon"}
            _LOG.info("cartogate-mcp: set_workspace -> %s (id=%s, in-process)", root, repo_id)
            # Bounded first build (see _bounded_prime; the lock is held for the inline bound — by
            # design: the first activation serializes, and concurrent callers get the progress
            # payload on their next try). On timeout we record the abandoned build in
            # state["indexing"] and answer with an explicit status so the call never out-waits the
            # client (the hang-then-empty failure mode). Later calls commit via _poll_indexing.
            box, t0 = await _bounded_prime(new_refresh)
            if "error" in box:
                _LOG.warning("cartogate-mcp: set_workspace failed to index %s (%s)",
                             root, box["error"])
                return {"ok": False, "error": f"could not index {root}: {box['error']}"}
            if "store" not in box:  # still building — detach and report instead of blocking
                state["indexing"] = {
                    "root": root, "repo_id": repo_id, "refresh": new_refresh, "box": box, "t0": t0,
                    "via": "agent",
                }
                _LOG.info("cartogate-mcp: first index of %s continues in the background", root)
                return {
                    "ok": True, "repo": str(root), "repo_id": repo_id, "status": "indexing",
                    "note": "first index of this repo is running in the background — retry your "
                    "call in ~30 seconds",
                }
            state["refresh"], state["repo"] = new_refresh, root
            state["tools"] = CartogateTools(box["store"], root=root)
            state["resolve_after"], daemon_on[0] = 0.0, False
            state["indexing"] = None  # this activation supersedes any detached build
            state["resolved_via"] = "agent"
            nodes = len(box["store"].visible_node_ids())
            _LOG.info("index ready: %d nodes in %.1fs (rss %s MB)",
                      nodes, time.monotonic() - t0, _rss_mb())
        return {"ok": True, "repo": str(root), "repo_id": repo_id, "nodes": nodes, "mode": "inproc"}

    async def _poll_indexing() -> dict[str, Any] | None:
        """Commit a finished detached first build, or report its progress.

        ``None`` means nothing is in flight (or it just committed) — proceed with the call. A dict
        is the payload to return instead: still-building status, or the build's failure.
        """
        if state["indexing"] is None:  # fast path — reference read, no lock
            return None
        async with lock:
            pending = state["indexing"]
            if pending is None:
                return None
            box = pending["box"]
            if "error" in box:
                state["indexing"] = None
                _LOG.warning("cartogate-mcp: background index of %s failed (%s)",
                             pending["root"], box["error"])
                return {
                    "error": f"indexing {pending['root']} failed: {box['error']}",
                    "action": SET_WORKSPACE_TOOL,
                }
            if "store" in box:  # done — commit and serve this very call
                state["refresh"], state["repo"] = pending["refresh"], pending["root"]
                state["tools"] = CartogateTools(box["store"], root=pending["root"])
                state["resolve_after"], daemon_on[0] = 0.0, False
                state["indexing"] = None
                state["resolved_via"] = pending.get("via", "agent")
                _LOG.info("index ready (background): %d nodes in %.1fs (rss %s MB)",
                          len(box["store"].visible_node_ids()),
                          time.monotonic() - pending["t0"], _rss_mb())
                return None
            return {
                "ok": True, "status": "indexing", "repo": str(pending["root"]),
                "repo_id": pending["repo_id"],  # consistent with _activate's ok-response shape
                "elapsed_s": round(time.monotonic() - pending["t0"], 1),
                "note": "first index of this repo is still building — retry in ~30 seconds",
            }

    async def _call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        name = params.name
        arguments = dict(params.arguments or {})
        # we may strip the self-config param; don't mutate the SDK dict
        rejected = validate_arguments(name, arguments)  # before workspace_root is popped
        if rejected is not None:
            return rejected
        if name == SET_WORKSPACE_TOOL:  # server-control tool: (re)target the workspace, then index
            activation_result = await _activate(arguments.get("root"))
            return _json_result(dict(activation_result))
        root_arg = arguments.pop(WORKSPACE_ROOT_PARAM, None)
        pending_status = await _poll_indexing()  # commit/report a detached first build FIRST
        if pending_status is not None:
            return _json_result(pending_status)
        retarget = False
        if isinstance(root_arg, str) and root_arg.strip():
            if state["refresh"] is None:
                retarget = True
            elif state.get("resolved_via") == "auto":
                # The workspace was AUTO-derived (roots/registry heuristics) — the agent naming
                # a DIFFERENT root knows its window better; honor it. Agent/pinned choices stay
                # sticky (set_workspace remains the explicit re-target for those).
                with contextlib.suppress(OSError, ValueError):
                    retarget = Path(root_arg).expanduser().resolve() != state["repo"]
        if retarget:
            # Deterministic first-call self-configuration: the agent passed its workspace root
            # on a tool call while the server was unresolved (or auto-anchored elsewhere) —
            # activate it (same path as set_workspace, incl. the warm-daemon preference), then
            # serve the requested tool below.
            activation = await _activate(root_arg)
            if activation.get("status") == "indexing":  # detached — report, don't block/serve
                return _json_result(dict(activation))
            if not activation.get("ok"):
                return _json_result(
                    {"error": f"workspace_root rejected: {activation.get('error')}",
                     "action": SET_WORKSPACE_TOOL}
                )
        async def _forward_to_daemon() -> dict[str, Any] | None:
            """Forward to the warm resolved daemon; ``None`` = daemon lost (flag flipped off)."""
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: daemon_client.query(
                        state["repo"], name, arguments, timeout=_DAEMON_QUERY_TIMEOUT
                    )
                )
                _LOG.info("tool %s -> daemon", name)
                return dict(result)
            except daemon_client.DaemonUnavailableError as exc:
                daemon_on[0] = False  # the daemon went away — serve in-process from here on
                _LOG.warning("daemon unavailable (%s); serving in-process for the session", exc)
                return None

        if daemon_on[0]:  # forward to the warm resolved daemon (shared graph)
            forwarded = await _forward_to_daemon()
            if forwarded is not None:
                return _json_result(forwarded)

        # ``ctx`` is the per-request ServerRequestContext the runner builds for every inbound
        # message; ``.session`` is the connection-scoped ServerSession that carries
        # server-to-client requests — here, the ``roots/list`` call the deferred workspace
        # resolver makes (see ``_repo_from_client_roots``). Always present on a real request.
        session = ctx.session
        await warm(session)  # build the graph (resolving the workspace first if deferred)
        if daemon_on[0]:
            # warm() resolved the workspace (roots/registry) and found its live daemon — forward.
            forwarded = await _forward_to_daemon()
            if forwarded is not None:
                return _json_result(forwarded)
            await warm(session)  # the daemon died in the same instant — build in-process (bounded)
        tools = state["tools"]
        if tools is None:
            # warm() may have DETACHED its first build (the bound elapsed) — report that progress,
            # not the misleading "unknown workspace" error below.
            pending_status = await _poll_indexing()
            if pending_status is not None:
                return _json_result(pending_status)
            tools = state["tools"]  # the poll may have committed a finished build — serve it
        if tools is None:  # deferred resolution failed — tell the agent how to fix it, don't hang
            msg = (
                "Cartogate doesn't know which project this is (the editor provides no workspace "
                f"signal). Retry with the '{WORKSPACE_ROOT_PARAM}' parameter set to the absolute "
                f"path of your workspace/repo root, or call '{SET_WORKSPACE_TOOL}' once. "
                "(Or pin CARTOGATE_REPO in the MCP config.)"
            )
            _LOG.warning("cartogate-mcp: %s", msg)
            payload: dict[str, Any] = {"error": msg, "action": SET_WORKSPACE_TOOL}
            # Several projects have live daemons -> auto-connect couldn't pick; list them so the
            # agent can name the one this window is in (the only per-window signal that exists).
            live = await anyio.to_thread.run_sync(registry.live_daemon_workspaces)
            if len(live) > 1:
                payload["known_workspaces"] = [str(p) for p in live]
            return _json_result(payload)
        # Run the query in a worker thread (so it never blocks the event loop) against the CURRENT
        # graph — never a synchronous rebuild. Background refresh keeps the graph fresh out of band.
        t0 = time.monotonic()
        result = await anyio.to_thread.run_sync(dispatch, tools, name, arguments)
        _LOG.info("tool %s -> %.3fs (in-process)", name, time.monotonic() - t0)
        return _json_result(result)

    server.add_request_handler(
        "tools/call", types.CallToolRequestParams, _guard_tool_errors(_call_tool)
    )
    return server, warm, refresh_once


async def _refresh_loop(refresh_once: _AsyncFn, poll_s: float = _REFRESH_POLL_S) -> None:
    """Poll for edits and rebuild out of band, so a (minutes-long, resolved) re-index never blocks a
    tool call. A rebuild *failure* is non-fatal — keep serving the last good graph. Cancellation is
    NOT swallowed: ``anyio``/``asyncio`` cancellation is a ``BaseException`` (not ``Exception``) on
    Python ≥ 3.8, so ``except Exception`` lets the scope cancel exit the loop cleanly on disconnect.
    """
    while True:
        await anyio.sleep(poll_s)
        try:
            await refresh_once()
        except Exception:  # noqa: BLE001 — see docstring; cancellation still propagates
            _LOG.warning(
                "cartogate: background refresh failed; serving the last good graph", exc_info=True
            )


def _decide_daemon(repo: Path, no_daemon: bool) -> bool:
    """Pick the daemon strategy. Returns True to forward tool calls to a warm resolved daemon.

    Prefer a daemon that's already up (the shared-graph win); otherwise — unless ``no_daemon`` —
    start one in the background for next session and serve in-process now (this session's fallback).
    """
    if no_daemon:
        return False
    if _resolved_daemon_ready(repo):
        _LOG.info("using the warm resolved daemon for this repo (shared graph)")
        return True
    _LOG.info("no resolved daemon up; serving in-process and starting one for next session")
    _spawn_resolved_daemon(repo)
    return False


async def _serve_lazy(
    refresh: RefreshStrategy | None,
    repo: Path | None,
    no_daemon: bool,
    *,
    resolver: _DeferredResolver | None = None,
) -> None:
    """Serve stdio at once. Prefer a warm resolved daemon (shared graph); else serve in-process and
    start a daemon for next session. ``no_daemon`` forces the in-process path. When ``repo`` is None
    (deferred), the daemon is skipped this session — the workspace isn't known until the first call.
    """
    # Off the event loop: _decide_daemon does blocking I/O (discovery read + a health socket probe).
    daemon_ready = (
        await anyio.to_thread.run_sync(_decide_daemon, repo, no_daemon)
        if repo is not None
        else False
    )
    server, warm, refresh_once = build_lazy_server(
        refresh, repo, daemon_ready=daemon_ready, resolver=resolver
    )

    async def _warm_bg() -> None:
        # A failed *background* pre-warm must not kill the server (an unhandled exception in a
        # task-group child would tear down server.run too). Swallow + log; the store stays unbuilt,
        # so the first tool call retries warm() and surfaces any persistent error to the client.
        try:
            await warm()
        except Exception:  # noqa: BLE001 — keep the server alive regardless of the index failure
            _LOG.warning(
                "cartogate: background index pre-warm failed; will retry on first tool call",
                exc_info=True,
            )

    async with anyio.create_task_group() as tg:
        if not daemon_ready:  # only build an in-process graph when we're not forwarding to a daemon
            tg.start_soon(_warm_bg)  # pre-build during the idle window after connect
        # The refresh loop is a no-op until the in-process graph is primed (on the daemon-loss
        # fallback), so it's harmless to run in daemon mode and ready if we ever fall back.
        tg.start_soon(_refresh_loop, refresh_once)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        # Client disconnected: stop the background tasks. A prime()/rebuild already running in a
        # worker thread (cancellable=False) finishes naturally before the process exits — harmless.
        tg.cancel_scope.cancel()


def _run_and_account_exit(serve: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run the server and make sure the LOG says how the process ended.

    "transport closed" in an editor is unexplainable unless the server records its own death: a
    clean client disconnect logs as such; an unhandled Python exception is logged with traceback
    (stderr alone is swallowed by most editors) and re-raised; a native fault is faulthandler's
    job (see :func:`enable_crash_visibility`).
    """
    try:
        anyio.run(serve)
    except BaseException as exc:
        _LOG.exception("cartogate-mcp: FATAL — server dying on unhandled %s", type(exc).__name__)
        raise
    _LOG.info("cartogate-mcp: transport closed by the client — exiting cleanly")


def main() -> None:
    """Console entry point: serve stdio at once, indexing the configured repo in the background.

    The repo to index is resolved by precedence: a **path argument** (``cartogate-mcp <repo>`` — so
    a client config can pass ``${workspaceFolder}`` with no hardcoded path), then ``CARTOGATE_REPO``
    env, then the launch dir's project root. That cwd fallback is **refused** when it lands on
    an editor/app install dir (a client that spawns the server without a workspace leaves cwd at its
    own dir) — the server exits with an actionable message rather than indexing the wrong tree.
    ``CARTOGATE_REPO_ID`` overrides the id (default the repo directory name). ``--no-daemon`` (or
    ``CARTOGATE_NO_DAEMON``) forces the in-process graph and never starts a daemon.
    """
    argv, env, cwd = sys.argv[1:], dict(os.environ), Path.cwd()
    no_daemon = _no_daemon_requested(argv, env)
    resolved = resolve_mcp_repo(argv, env, cwd)
    if resolved is None:
        # Deferred: the workspace wasn't pinned (arg/env) and cwd is an editor install dir. Don't
        # exit — serve, and resolve the workspace from the client's MCP roots on the first call.
        # This is what makes it work per-window, with no hardcoded path, across multiple projects.
        configure_logging(None)  # -> ~/.cartogate/mcp.log until the repo is known
        enable_crash_visibility(ensure_state_dir(Path.home()) / "mcp.log")
        _log_env_diagnostics(env)
        _LOG.info(
            "cartogate-mcp %s: workspace not pinned — resolving from the client (MCP roots) on"
            " first tool call%s", cartogate.__version__, " [--no-daemon]" if no_daemon else "",
        )
        resolver = _make_deferred_resolver(env, cwd)
        _run_and_account_exit(
            functools.partial(_serve_lazy, None, None, no_daemon, resolver=resolver)
        )
        return
    repo, repo_id = resolved
    if not repo.is_dir():  # a typo'd arg/env would otherwise index an empty phantom tree
        sys.stderr.write(f"cartogate-mcp: repository path does not exist: {repo}\n")
        raise SystemExit(2)
    configure_logging(repo)
    enable_crash_visibility(ensure_state_dir(repo) / "mcp.log")
    registry.register_workspace(repo)
    _LOG.info(
        "cartogate-mcp %s serving %s (id=%s)%s",
        cartogate.__version__, repo, repo_id, " [--no-daemon]" if no_daemon else "",
    )
    # A resolved (full-graph, doc-aware) refresher so the rich tools stay current as files change.
    refresh = GitLazyRefresh(repo, repo_id=repo_id, resolve=True, index_docs=True)
    _run_and_account_exit(functools.partial(_serve_lazy, refresh, repo, no_daemon))




if __name__ == "__main__":
    main()
