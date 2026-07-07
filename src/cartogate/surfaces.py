"""Shared logic for the agent-agnostic enforcement surfaces (spec §7.3–§7.4).

The git pre-commit hook and the Claude Code / Codex ``PreToolUse`` hook both need the same
core: pull the symbols a change proposes to add, and ask the gate whether any duplicates an
existing one. Keeping that here (not in the hook scripts) makes it unit-testable and keeps
the scripts to a thin stdin/exit-code shell.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cartogate.mcp.tools import CartogateTools
from cartogate.schema.enums import Language, NodeKind
from cartogate.schema.nodes import Node
from cartogate.schema.signature import normalize_signature
from cartogate.store.base import StoreInterface

#: Tool-input keys that may carry the text an agent proposes to write. Covers the common
#: Claude Code / Codex Write/Edit shapes; unrecognized tool schemas yield no text (the gate
#: then allows the call — fail-open, consistent with the PreToolUse posture).
_PROPOSED_TEXT_KEYS = ("content", "new_string", "new_str", "text")

#: Files/dirs whose presence marks a directory as a project root. ``.git`` first (the universal
#: signal — a dir for a normal clone, a *file* for a worktree/submodule), then common
#: language-ecosystem manifests so a non-git checkout still resolves.
_ROOT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    ".hg",
    ".svn",
)


#: Path fragments that mark a directory as an editor's own install/data location rather than a user
#: project. Two ways a client hands us one: (1) it spawns the MCP server with cwd = the editor's
#: install dir (no workspace set); (2) newer Windsurf advertises MCP ``roots`` but reports its own
#: per-user data dir ``~/.codeium/windsurf`` as the "workspace" — indexing that huge, binary-laden
#: tree hangs the gate. Either way, auto-detecting a repo from these indexes the wrong tree, so we
#: refuse and fall back to ``set_workspace`` / ``CARTOGATE_REPO``. Matched case-insensitively in the
#: POSIX path; specific enough that a real project path never contains them. Best-effort: Windows
#: (AppData\\Local\\Programs, Program Files) + macOS (root /Applications, handled below) installs,
#: and the ``.codeium`` data dir. Linux portable installs (snap/flatpak/~/.local) aren't covered —
#: pass the repo path / set CARTOGATE_REPO there.
_EDITOR_INSTALL_MARKERS = (
    "/appdata/local/programs/",
    "/program files/",
    "/program files (x86)/",
    "/.codeium/",  # Windsurf/Codeium per-user DATA dir (e.g. ~/.codeium/windsurf), NOT a project
)


def looks_like_editor_install(path: Path) -> bool:
    """True if ``path`` looks like an editor's own install/data directory, not a user's project.

    Used to refuse silently indexing the editor's own tree — either the cwd a client hands the
    server when it doesn't set a workspace, or a bogus MCP ``root`` a client reports (newer Windsurf
    reports its ``~/.codeium/windsurf`` data dir). In both cases the server asks for the workspace
    instead (``set_workspace`` / ``CARTOGATE_REPO``).
    """
    p = path.as_posix().lower()
    # macOS app bundles live at the *root* ``/Applications/`` — anchor it so a user's own
    # ``~/applications/my-repo`` project isn't mistaken for one.
    if p.startswith("/applications/"):
        return True
    return any(marker in p for marker in _EDITOR_INSTALL_MARKERS)


def find_repo_root(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` (inclusive) that looks like a project root, or ``None``.

    Walks up from ``start`` (a file or directory) to the first directory containing a
    :data:`_ROOT_MARKERS` entry. Letting the gate discover the root from the file being edited is
    what makes a *single* global hook config gate every repo you open — no per-repo path to pin.
    """
    start = start if start.is_dir() else start.parent
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory
    return None


def resolve_repo(file_path: str | None, *, env: Mapping[str, str], cwd: Path) -> tuple[Path, str]:
    """Decide which repo the gate / MCP server should index, and its id.

    Precedence, explicit → automatic:

    1. ``CARTOGATE_REPO`` (an explicit pin — used by the value study and anyone who wants a fixed
       graph) wins outright.
    2. Otherwise the enclosing **project root** of the file being edited (or of ``cwd`` when no
       file is in play, e.g. the MCP server at launch), via :func:`find_repo_root`.
    3. Otherwise ``cwd``.

    The id is ``CARTOGATE_REPO_ID`` if set, else the chosen directory's name.
    """
    override = env.get("CARTOGATE_REPO")
    if override:
        repo = Path(override).expanduser().resolve()
    else:
        anchor = Path(file_path).expanduser().resolve() if file_path else cwd.resolve()
        root = find_repo_root(anchor)
        repo = root if root is not None else cwd.resolve()
    repo_id = env.get("CARTOGATE_REPO_ID") or repo.name
    return repo, repo_id


def find_duplicate_signatures(nodes: list[Node]) -> dict[tuple[Language, str], list[Node]]:
    """Group symbols by ``(language, normalized signature)``, keeping only true duplicates.

    A duplicate is a normalized signature shared by two or more *distinct* qualified names
    (the same function written twice) — not a symbol matching itself. Grouping includes the
    language so a Python and a TypeScript symbol with the same shape are not flagged as
    duplicates. Only top-level functions/classes are considered: methods of different classes
    legitimately share signatures (an ABC and its impl, unrelated ``__init__``s).
    """
    groups: dict[tuple[Language, str], list[Node]] = defaultdict(list)
    for node in nodes:
        if node.kind is NodeKind.SYMBOL and node.signature is not None and node.is_top_level:
            groups[(node.language, normalize_signature(node.signature, node.language))].append(node)
    return {
        key: members
        for key, members in groups.items()
        if len({m.qualified_name for m in members}) > 1 and _is_real_duplication(members)
    }


def _is_real_duplication(members: list[Node]) -> bool:
    """Callable pairs duplicate by signature alone; TYPE DECLARATIONS need a shared body hash.

    Field evidence (2026-07-04): per-component React ``Props`` and per-service ``Settings``
    share signatures idiomatically — name + bases is too weak to block a class on. Identical
    bodies (true copy-paste) still count; callables are unchanged (a re-implementation with a
    different body is exactly what the gate exists to catch).
    """
    callables = [m for m in members if not m.is_type_decl]
    if len({m.qualified_name for m in callables}) > 1:
        return True
    # Type declarations compare only with type declarations (a callable homonym is a naming
    # coincidence): copy-paste = a body hash shared by two distinct type-decl qnames.
    by_body: dict[str, set[str]] = {}
    for m in members:
        if m.is_type_decl and m.body_hash is not None:
            by_body.setdefault(m.body_hash, set()).add(m.qualified_name)
    return any(len(qnames) > 1 for qnames in by_body.values())


def gate_proposed_source(
    store: StoreInterface,
    source: str,
    language: Language = Language.PYTHON,
    *,
    editing_unit: str | None = None,
) -> list[dict[str, Any]]:
    """Return the blocked duplicate verdicts for every symbol the snippet would add.

    ``editing_unit`` is the unit (file) being edited, if any: a symbol that already lives there is
    the one being edited, not a new duplicate, so it does not self-block (F-28).
    """
    from cartogate.extract.languages import symbol_facts_in

    tools = CartogateTools(store)
    verdicts = (
        tools.check_duplicate(
            sym.signature,
            language=language.value,
            exclude_unit=editing_unit,
            proposed_body_hash=sym.body_hash,
            proposed_is_type_decl=sym.is_type_decl,
        )
        for sym in symbol_facts_in(source, language)
    )
    return [verdict for verdict in verdicts if verdict["blocked"]]


def extract_proposed_text(tool_input: dict[str, Any]) -> str:
    """Pull the proposed source text out of a PreToolUse tool-input payload."""
    parts = [tool_input[key] for key in _PROPOSED_TEXT_KEYS if isinstance(tool_input.get(key), str)]
    return "\n".join(parts)
