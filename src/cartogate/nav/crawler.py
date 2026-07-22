"""nav crawler, verify+propose mode (Stage 3; design: 2026-07-20 crawler spec).

Visits each DECLARED param-free state live, verifies declared landmarks, and
proposes landmarks/affordances (``crawled`` provenance) for states that lack
them. Proposals are quarantined in ``<map>.proposed.json`` — the live map is
never modified; merging proposals into it is the human act of approval.
Discovery beyond the declared map is a separate opt-in mode (``--discover``),
built on top of this and gated by its own controls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cartogate.nav.driver import Driver, Target
from cartogate.nav.runtime import _url_params_count
from cartogate.nav.schema import Landmark, load


@dataclass
class CrawlReport:
    """What a verify+propose crawl did — every skip and refusal named."""

    visited: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    verified: list[tuple[str, bool]] = field(default_factory=list)
    proposed_path: Path | None = None


def _landmark_verified(driver: Driver, landmark: Landmark) -> bool:
    """One landmark's live truth: visible, and checked state matches if declared."""
    target = Target(role=landmark.role, name=landmark.name)
    if not driver.is_visible(target):
        return False
    return landmark.checked is None or driver.is_checked(target) == landmark.checked


def crawl_verify_propose(map_path: Path, driver: Driver) -> CrawlReport:
    """Verify declared states live; propose what the map lacks.

    Accepts drafts (empty landmarks — the `cartogate navmap` seed shape).
    Deterministic: states visited in declared order; proposals sorted by the
    inventory's own order (the page's document order as reported).
    """
    navmap = load(map_path, draft=True)
    raw = json.loads(map_path.read_text(encoding="utf-8"))
    report = CrawlReport()

    proposed_states: list[dict[str, Any]] = []
    for state in navmap.states:
        state_raw: dict[str, Any] = {
            "id": state.id,
            "url": state.url,
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

        if _url_params_count(state.url) > 0:
            report.skipped.append((state.id, "url has :params and no declared path"))
            proposed_states.append(state_raw)
            continue

        driver.navigate(state.url)
        report.visited.append(state.id)

        needs_inventory = not state.landmarks or not state.affordances
        inventory = driver.page_inventory() if needs_inventory else {}

        if state.landmarks:
            ok = all(_landmark_verified(driver, lm) for lm in state.landmarks)
            report.verified.append((state.id, ok))
        else:
            for candidate in inventory.get("landmarks", []):
                state_raw["landmarks"].append({**candidate, "provenance": "crawled"})

        if not state.affordances:
            for i, candidate in enumerate(inventory.get("affordances", []), start=1):
                state_raw["affordances"].append(
                    {"ref": f"c{i}", **candidate, "provenance": "crawled"}
                )

        proposed_states.append(state_raw)

    proposal = {
        "comment": (
            "Crawled proposals — review, edit, and merge into the map yourself; "
            "crawled facts are runtime-observed and never authoritative."
        ),
        "version": raw.get("version", 1),
        "app": raw.get("app", ""),
        "states": proposed_states,
        "transitions": raw.get("transitions", []),
        "flows": raw.get("flows", []),
    }
    proposed_path = map_path.with_suffix(".proposed.json")
    proposed_path.write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report.proposed_path = proposed_path
    return report
