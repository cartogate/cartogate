"""Documentation extractor — explicit doc→code references (spec §5.2).

Deterministic and model-free, mirroring the structural pass. It parses markdown for *explicit*
references to code — inline code spans (`` `authenticate` ``) and links to source files
(``](pkg/auth.py)``) — and emits ``doc_section`` nodes + ``documents`` edges. Conceptual/fuzzy
mentions are deliberately ignored: a reference counts only if it unambiguously identifies a
symbol or module. Doc facts are ``EXTRACTED`` but ride ``Provenance.DOC`` (not in
``BLOCKABLE_PROVENANCES``) and the ``documents`` edge type (not in ``GATE_EDGE_TYPES``), so they
can never reach the gate — they power the advisory ``doc_drift`` report only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path

from cartogate.extract.walk import iter_files
from cartogate.schema.edges import Edge, SourceLocation
from cartogate.schema.enums import Confidence, EdgeType, NodeKind, Provenance, Visibility
from cartogate.schema.nodes import Location, Node

#: Inline code span: `text` (single-line).
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: A markdown link to a source file: ](path.py|.ts|.tsx) optionally with #anchor/?query.
_SOURCE_LINK = re.compile(r"\]\(\s*([^)\s]+\.(?:py|tsx|ts))(?:[#?][^)\s]*)?\s*\)")


@dataclass(slots=True)
class DocFacts:
    """Doc nodes + documents edges produced from a doc tree."""

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


class SymbolIndex:
    """Lookups for conservatively matching a doc reference to a symbol or module."""

    def __init__(self, symbols: list[Node], *, modules: list[Node]) -> None:
        self._by_qname: dict[str, Node] = {s.qualified_name: s for s in symbols}
        self._by_name: dict[str, list[Node]] = {}
        for sym in symbols:
            self._by_name.setdefault(sym.name, []).append(sym)
        # Module nodes keyed by their unit (POSIX rel path) for link resolution.
        self._modules: dict[str, Node] = {m.unit: m for m in modules}

    def match_span(self, span: str) -> Node | None:
        """Match an inline code span to a symbol — exact qname, or a *unique* bare name."""
        text = span.strip()
        if text.endswith("()"):
            text = text[:-2].strip()
        if text in self._by_qname:
            return self._by_qname[text]
        candidates = self._by_name.get(text, [])
        return candidates[0] if len(candidates) == 1 else None  # ambiguous -> skip

    def match_link(self, link: str) -> Node | None:
        """Match a ``](path.py)`` link to a module — exact unit, or a *unique* path suffix."""
        target = link.strip().lstrip("./")
        if target in self._modules:
            return self._modules[target]
        candidates = [m for unit, m in self._modules.items() if unit.endswith("/" + target)]
        return candidates[0] if len(candidates) == 1 else None


def extract_doc_facts(
    root: Path,
    *,
    repo_id: str,
    base: Path,
    symbols: list[Node],
    modules: list[Node],
    allow: list[Path] | None = None,
) -> DocFacts:
    """Parse markdown under ``root`` into doc_section nodes + documents edges.

    ``allow`` is the git working set (:func:`~cartogate.extract.pipeline.git_tracked_files`) — the
    doc pass respects ``.gitignore`` exactly like the source pass, so vendored trees
    (``node_modules`` and friends) are never even walked for markdown.
    """
    index = SymbolIndex(symbols, modules=modules)
    base = base.resolve()
    facts = DocFacts()

    for path in iter_files(root, ".md", allow):
        rel = path.resolve().relative_to(base).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")

        targets: set[str] = set()
        for span in _CODE_SPAN.findall(text):
            match = index.match_span(span)
            if match is not None:
                targets.add(match.id)
        for link in _SOURCE_LINK.findall(text):
            match = index.match_link(link)
            if match is not None:
                targets.add(match.id)

        if not targets:
            continue  # a doc that references nothing is not worth a node

        # v0 granularity = one doc_section per file (per-heading sections are a refinement, F-44).
        doc_node = Node.create(
            repo_id=repo_id,
            qualified_name=rel,
            kind=NodeKind.DOC_SECTION,
            name=path.name,
            unit=rel,
            location=Location(path=rel, start_line=1, end_line=text.count("\n") + 1),
            provenance=Provenance.DOC,
            confidence=Confidence.EXTRACTED,
            content_hash=blake2b(text.encode("utf-8"), digest_size=16).hexdigest(),
            visibility=Visibility.PUBLIC,
        )
        facts.nodes.append(doc_node)
        for target_id in sorted(targets):
            facts.edges.append(
                Edge(
                    type=EdgeType.DOCUMENTS,
                    src=doc_node.id,
                    dst=target_id,
                    provenance=Provenance.DOC,
                    confidence=Confidence.EXTRACTED,
                    source_location=SourceLocation(path=rel, line=1),
                )
            )
    return facts
