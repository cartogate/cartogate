"""Graph visualization / export — see what Cartogate extracted.

Read-only and additive: serializes the graph (nodes + edges from an ``IndexResult``) to
portable formats. None of it touches the gate.

- ``export.to_graphml`` / ``export.to_json`` — open in Gephi/yEd or any graph tool.
- ``html.to_html`` — a single self-contained, offline interactive view (pan/zoom/filter).
"""

from cartogate.viz.export import to_graphml, to_json
from cartogate.viz.html import to_html

__all__ = ["to_graphml", "to_html", "to_json"]
