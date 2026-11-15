"""Shared serialization for crawl proposal documents.

Both ``nav crawl`` (verify+propose, crawler.py) and ``nav crawl --discover``
(discover.py) quarantine their output in ``<map>.proposed.json`` next to the
live map. The seed-state echo and the document envelope must stay
byte-compatible between the two producers so a reviewer sees ONE format —
this module is the single writer both import (they previously carried
diverging copies).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cartogate.nav.schema import State


def echo_seed_state(state: State) -> dict[str, Any]:
    """A declared state re-serialized for a proposal doc (round-trips the schema fields;
    optional fields are emitted only when set, matching the hand-authored map style)."""
    return {
        "id": state.id,
        "url": state.url,
        "provenance": state.provenance,
        "landmarks": [
            {
                "role": lm.role,
                "name": lm.name,
                **({"checked": lm.checked} if lm.checked is not None else {}),
                "provenance": lm.provenance,
            }
            for lm in state.landmarks
        ],
        "affordances": [
            {
                "ref": aff.ref,
                "role": aff.role,
                "name": aff.name,
                **({"css": aff.css} if aff.css else {}),
                "provenance": aff.provenance,
            }
            for aff in state.affordances
        ],
    }


def write_proposal_doc(
    map_path: Path,
    raw: dict[str, Any],
    states: list[dict[str, Any]],
    *,
    comment: str,
    transitions: list[Any],
) -> Path:
    """Write the quarantined proposal document beside the map; returns its path.

    ``raw`` is the live map's parsed JSON — version/app/flows are echoed from it
    so the proposal can be diffed against (and merged into) the map it grew from.
    The live map itself is never written (merging is the human act of approval).
    """
    doc = {
        "comment": comment,
        "version": raw.get("version", 1),
        "app": raw.get("app", ""),
        "states": states,
        "transitions": transitions,
        "flows": raw.get("flows", []),
    }
    out = map_path.with_suffix(".proposed.json")
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
