"""Deterministic "what you'd do without Cartogate" counterfactuals.

The honest comparison for every gate/navigation claim is *the realistic naive approach a
developer (or a coding agent without the graph) actually uses*: text search. These helpers
implement that — a word-boundary ``grep`` over the source/doc tree — so the study measures
Cartogate against a real baseline, not a strawman.

Their known weaknesses (matching comments, strings, and same-named-but-different symbols;
needing the literal name to appear) are exactly the failure modes the graph avoids, and the
study reports them as measured numbers rather than asserting them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path


def iter_source_files(root: Path, suffixes: tuple[str, ...] = (".py", ".ts")) -> Iterator[Path]:
    """Yield source files under ``root`` (skipping the usual noise dirs)."""
    skip = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in suffixes and not skip.intersection(path.parts):
            yield path


def _word_search(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def units_referencing(
    root: Path, name: str, suffixes: tuple[str, ...] = (".py", ".ts")
) -> set[str]:
    """Grep baseline for "which files reference X": POSIX relpaths whose text contains ``name``.

    This is the naive answer to "what depends on this symbol" — it cannot tell a real call
    from the same word in a comment, a string, or an unrelated symbol of the same name.
    """
    out: set[str] = set()
    for path in iter_source_files(root, suffixes):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _word_search(text, name):
            out.add(path.relative_to(root).as_posix())
    return out


def defines_callable(
    root: Path, name: str, suffixes: tuple[str, ...] = (".py", ".ts")
) -> bool:
    """Naive duplicate check: does *any* ``def``/``class``/``function``/``func`` of ``name`` exist?

    This is what ``grep "def name("`` finds — at any indentation, so it also matches a
    *method* of that name (the dogfood false-positive class) and a same-named function with
    a different parameter list, neither of which is a real top-level duplicate.
    """
    pattern = re.compile(rf"^\s*(?:async\s+def|def|class|function|func)\s+{re.escape(name)}\b")
    for path in iter_source_files(root, suffixes):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(pattern.match(line) for line in text.splitlines()):
            return True
    return False


def docs_referencing(doc_root: Path, name: str) -> set[str]:
    """Grep baseline for doc-drift: markdown files whose text contains ``name`` anywhere.

    Over-matches prose mentions and same-named-but-different symbols; under-matches docs
    that link to the file without naming the symbol.
    """
    out: set[str] = set()
    for path in sorted(doc_root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _word_search(text, name):
            out.add(path.relative_to(doc_root).as_posix())
    return out
