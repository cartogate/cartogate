"""SCIP-shaped index emission (spec §5.1).

SCIP (Sourcegraph Code Intelligence Protocol) is the portable, tool-agnostic index
interchange format the spec asks v0 to emit. The canonical wire form is protobuf; v0
emits the same *model* (Index → Documents → Symbols/Occurrences with SCIP-style monikers)
as JSON, which is dependency-free, air-gapped, and trivially inspectable. The own graph
remains the source of truth — this is a thin serializer over it (risk R5), and the protobuf
encoder plus a ``scip-python`` cross-check oracle are logged as future work.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cartogate.extract.pipeline import IndexResult
from cartogate.schema.enums import NodeKind

#: Node kinds that are emitted as SCIP documents' declared symbols.
_DOCUMENT_SYMBOL_KINDS = {NodeKind.MODULE, NodeKind.SYMBOL}


@dataclass(slots=True)
class _ScipDocument:
    """One SCIP document (a source file) with its declared symbols and occurrences."""

    relative_path: str
    language: str  # the source language, taken from the nodes' `language` tag (not assumed)
    symbols: list[dict[str, str]] = field(default_factory=list)
    occurrences: list[dict[str, object]] = field(default_factory=list)


def emit_scip(result: IndexResult, out_path: Path, *, repo_id: str) -> None:
    """Serialize ``result`` to a SCIP-shaped JSON index at ``out_path``."""
    documents: dict[str, _ScipDocument] = {}

    for node in result.nodes:
        if node.kind not in _DOCUMENT_SYMBOL_KINDS:
            continue
        language = node.language.value
        doc = documents.setdefault(
            node.location.path, _ScipDocument(node.location.path, language)
        )
        moniker = _moniker(repo_id, node.qualified_name, language)
        doc.symbols.append({"symbol": moniker, "kind": node.kind.value})
        doc.occurrences.append(
            {
                "symbol": moniker,
                "range": [node.location.start_line, node.location.end_line],
                "role": "definition",
            }
        )

    index = {
        "tool": {"name": "cartogate", "version": _tool_version()},
        "encoding": "scip-shaped-json",
        "project_root": repo_id,
        "documents": [asdict(doc) for doc in documents.values()],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")


def _moniker(repo_id: str, qualified_name: str, language: str) -> str:
    # SCIP-style symbol moniker: scheme (scip-<language>) + package + descriptor.
    return f"scip-{language} cartogate {repo_id} {qualified_name}."


def _tool_version() -> str:
    from cartogate import __version__

    return __version__
