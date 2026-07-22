"""Navigation map schema: parse, validate, hash (spec §4).

A navigation map is a per-app, machine-checkable declaration of UI states, landmarks,
affordances (clickable elements), and transitions between states. Validation REFUSES
malformed input (unknown keys, missing landmarks, circular references) — refusal at
declaration time costs nothing and is always actionable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cartogate.hashing import canonical_blake2b


class NavMapError(ValueError):
    """A malformed navigation map — refused at load time."""


@dataclass(frozen=True)
class Affordance:
    """An interactive element (button, link, input, etc.) on a state.

    At least one of role, name, or css must be present.
    - role and name are semantically stable (ARIA/accessibility standards).
    - css is a fallback (less stable, but useful for testing/backwards-compat).
    """

    ref: str  # stable reference (e.g., "a1"), Playwright MCP style
    role: str | None = None  # ARIA role (e.g., "button")
    name: str | None = None  # ARIA-accessible name (e.g., "Click me")
    css: str | None = None  # CSS selector fallback
    provenance: str = "extracted"  # extracted | crawled | declared


@dataclass(frozen=True)
class Landmark:
    """A visual proof you're on the right state: role+name pair, optionally with checked state.

    For radio/checkbox elements, checked can verify the element is in the correct state.
    """

    role: str
    name: str
    checked: bool | None = None  # optional: radio/checkbox/aria-checked state
    provenance: str = "declared"  # declared | extracted | crawled (crawler proposals)


@dataclass(frozen=True)
class State:
    """A single UI state: URL pattern, visual landmarks, and available actions (affordances)."""

    id: str  # stable, dotted, human-named (e.g., "billing.invoices")
    url: str  # URL pattern; params allowed ("/inv/:id")
    landmarks: tuple[Landmark, ...]  # ≥1 visual proof you're here
    affordances: tuple[Affordance, ...]  # available interactive elements
    fragment: tuple[tuple[str, str], ...] = ()  # optional fragment kv pairs (sorted by key)
    provenance: str = "extracted"  # extracted | crawled | declared


@dataclass(frozen=True)
class Transition:
    """A single navigation edge: click a button or fill a form, then verify landmarks."""

    from_state: str  # source state id
    do: dict[str, Any]  # action: {"click": "ref"} or {"fill": ["ref", "text"]}
    to_state: str  # target state id


@dataclass(frozen=True)
class Flow:
    """A named sequence of states to traverse (e.g., "happy path" for checkout)."""

    name: str
    path: tuple[str, ...]  # tuple of state ids


@dataclass(frozen=True)
class NavMap:
    """A validated navigation map: all states, transitions, and flows verified."""

    app: str
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]
    flows: tuple[Flow, ...]
    raw: dict[str, Any]  # exact input dict — for hashing, logging, etc.
    entry_url: str | None = None  # optional entry point URL
    entry_actions: tuple[str, ...] = ()  # optional entry point actions

    def state(self, state_id: str) -> State:
        """Look up a state by id. Raises NavMapError if not found."""
        for state in self.states:
            if state.id == state_id:
                return state
        raise NavMapError(f"state {state_id!r} not found in map")

    @property
    def flows_by_name(self) -> dict[str, Flow]:
        """Flows indexed by name."""
        return {flow.name: flow for flow in self.flows}


def map_hash(raw: dict[str, Any]) -> str:
    """blake2b hex over canonical JSON (sorted keys, no whitespace) — key-order-free."""
    return canonical_blake2b(raw)


# "comment" is an explicitly-allowed, semantically-ignored documentation field —
# the crawler/discover proposal writers emit it, and a proposed map MUST round-trip
# through this parser so a human can `nav check` after review (review Critical
# 2026-07-21: unknown-key refusal made every proposal file unparseable).
_TOP_KEYS = {
    "version", "app", "map_hash", "entry", "states", "transitions", "flows", "comment",
}
_STATE_KEYS = {"id", "url", "landmarks", "affordances", "provenance"}
_LANDMARK_KEYS = {"role", "name", "checked", "provenance"}
_AFFORDANCE_KEYS = {"ref", "role", "name", "css", "provenance"}
_TRANSITION_KEYS = {"from", "do", "to"}
_FLOW_KEYS = {"name", "path"}
_VALID_PROVENANCES = {"extracted", "crawled", "declared"}


def _check_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    """Validate that data contains only allowed keys.

    Args:
        data: Dictionary to check.
        allowed: Set of allowed keys.
        where: Context for error message.

    Raises:
        NavMapError if unknown keys are found.
    """
    bad_keys = set(data.keys()) - allowed
    if bad_keys:
        raise NavMapError(f"{where} unknown key(s): {', '.join(sorted(bad_keys))}")


def _validate_provenance(value: object, where: str) -> str:
    """Validate that provenance is one of: extracted, crawled, declared.

    Args:
        value: Provenance value to validate.
        where: Context for error message (e.g., "states[0].provenance").

    Returns:
        The validated provenance string.

    Raises:
        NavMapError if provenance is invalid.
    """
    if not isinstance(value, str) or value not in _VALID_PROVENANCES:
        raise NavMapError(
            f"{where} must be one of: extracted, crawled, declared (got {value!r})"
        )
    return value


def _parse_fragment(url: str, where: str) -> tuple[tuple[str, str], ...]:
    """Parse and validate URL fragment into sorted (key, value) pairs.

    Fragment format: #k1=v1&k2=v2 (non-empty k and v, no duplicate keys).
    Junk segments without '=' are refused.

    Args:
        url: Full URL that may contain fragment (after #).
        where: Context for error message.

    Returns:
        Sorted tuple of (key, value) pairs, or () if no fragment.

    Raises:
        NavMapError if fragment is malformed.
    """
    if "#" not in url:
        return ()

    # Split on FIRST # only
    path_part, fragment_part = url.split("#", 1)

    if not fragment_part:
        # Empty fragment (e.g., "/a#") is treated as no fragment
        return ()

    # Parse fragment segments
    pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for segment in fragment_part.split("&"):
        if not segment:
            # Empty segment (e.g., "v=x&&s=y") — skip it
            continue

        if "=" not in segment:
            raise NavMapError(
                f"{where} fragment segment {segment!r} missing '=' "
                "(format: #k1=v1&k2=v2)"
            )

        key, value = segment.split("=", 1)

        if not key:
            raise NavMapError(
                f"{where} fragment has empty key in {segment!r} (format: #k1=v1&k2=v2)"
            )

        if not value:
            raise NavMapError(
                f"{where} fragment has empty value in {segment!r} (format: #k1=v1&k2=v2)"
            )

        if key in seen_keys:
            raise NavMapError(
                f"{where} fragment has duplicate key {key!r} (format: #k1=v1&k2=v2)"
            )

        seen_keys.add(key)
        pairs.append((key, value))

    # Sort by key for determinism
    pairs.sort(key=lambda x: x[0])
    return tuple(pairs)


def parse_navmap(data: object, *, draft: bool = False) -> NavMap:
    """Parse and validate a raw navigation map dict.

    ``draft=True`` relaxes exactly ONE rule: states may have empty landmarks.
    That is the `cartogate navmap` seed shape — landmarks are unextractable,
    and the CRAWLER exists to propose them. Every other refusal stays intact;
    runtime consumers (check/capture) always parse strict.

    Raises NavMapError for:
    - Invalid JSON structure
    - Unknown keys at any level
    - Missing required fields
    - Invalid state references
    - Mismatched or tampered map_hash
    """
    if not isinstance(data, dict):
        raise NavMapError("map must be a JSON object")

    # Check top-level keys
    _check_keys(data, _TOP_KEYS, "top-level")

    # Version is required and must be 1
    version = data.get("version")
    if version != 1:
        raise NavMapError("version must be exactly 1")

    # App is required
    app = data.get("app")
    if not isinstance(app, str) or not app.strip():
        raise NavMapError("app must be a non-empty string")

    # Self-check map_hash if present
    if "map_hash" in data:
        provided_hash = data["map_hash"]
        if not isinstance(provided_hash, str):
            raise NavMapError("map_hash must be a string")
        # Compute hash over the map without map_hash
        body = {k: v for k, v in data.items() if k != "map_hash"}
        expected_hash = map_hash(body)
        if provided_hash != expected_hash:
            raise NavMapError(
                f"map_hash self-check failed: provided {provided_hash[:16]}... "
                f"but body hashes to {expected_hash[:16]}..."
            )

    # Entry (optional): validate if present
    entry_url: str | None = None
    entry_actions: tuple[str, ...] = ()
    if "entry" in data:
        entry_data = data["entry"]
        if not isinstance(entry_data, dict):
            raise NavMapError("entry must be an object")
        _check_keys(entry_data, {"url", "actions"}, "entry")
        # url is required
        entry_url_val = entry_data.get("url")
        if not isinstance(entry_url_val, str) or not entry_url_val.strip():
            raise NavMapError("entry.url must be a non-empty string")
        entry_url = entry_url_val
        # actions is optional and defaults to []
        entry_actions_val = entry_data.get("actions", [])
        if not isinstance(entry_actions_val, list):
            raise NavMapError("entry.actions must be a list")
        # Validate that all actions are non-empty strings
        for j, action in enumerate(entry_actions_val):
            if not isinstance(action, str) or not action.strip():
                raise NavMapError(f"entry.actions[{j}] must be a non-empty string")
        entry_actions = tuple(entry_actions_val)

    # States are required
    states_data = data.get("states")
    if not isinstance(states_data, list):
        raise NavMapError("states must be a list")

    if not states_data:
        raise NavMapError("map must have at least one state")

    states_by_id: dict[str, State] = {}
    states: list[State] = []

    for i, state_data in enumerate(states_data):
        if not isinstance(state_data, dict):
            raise NavMapError(f"states[{i}] must be an object")

        _check_keys(state_data, _STATE_KEYS, f"states[{i}]")

        state_id = state_data.get("id")
        if not isinstance(state_id, str) or not state_id.strip():
            raise NavMapError(f"states[{i}].id must be a non-empty string")

        # Check for duplicate ids
        if state_id in states_by_id:
            raise NavMapError(f"duplicate state id: {state_id!r}")

        url = state_data.get("url")
        if not isinstance(url, str) or not url.strip():
            raise NavMapError(f"states[{i}].url must be a non-empty string")

        # Landmarks: required and must have ≥1
        landmarks_data = state_data.get("landmarks")
        if not isinstance(landmarks_data, list):
            raise NavMapError(f"states[{i}].landmarks must be a list")

        if not landmarks_data and not draft:
            raise NavMapError(
                f"state {state_id!r} must have at least one landmark "
                "(proves you're on the right page)"
            )

        landmarks: list[Landmark] = []
        for j, lm_data in enumerate(landmarks_data):
            if not isinstance(lm_data, dict):
                raise NavMapError(f"states[{i}].landmarks[{j}] must be an object")

            _check_keys(lm_data, _LANDMARK_KEYS, f"states[{i}].landmarks[{j}]")

            lm_role = lm_data.get("role")
            if not isinstance(lm_role, str) or not lm_role.strip():
                raise NavMapError(f"states[{i}].landmarks[{j}].role must be a non-empty string")

            lm_name = lm_data.get("name")
            if not isinstance(lm_name, str) or not lm_name.strip():
                raise NavMapError(f"states[{i}].landmarks[{j}].name must be a non-empty string")

            # checked is optional (bool or absent)
            lm_checked = lm_data.get("checked")
            if lm_checked is not None and not isinstance(lm_checked, bool):
                raise NavMapError(
                    f"states[{i}].landmarks[{j}].checked must be a boolean or absent "
                    f"(got {type(lm_checked).__name__})"
                )

            lm_provenance = _validate_provenance(
                lm_data.get("provenance", "declared"),
                f"states[{i}].landmarks[{j}].provenance",
            )
            landmarks.append(
                Landmark(
                    role=lm_role,
                    name=lm_name,
                    checked=lm_checked,
                    provenance=lm_provenance,
                )
            )

        # Affordances
        affordances_data = state_data.get("affordances", [])
        if not isinstance(affordances_data, list):
            raise NavMapError(f"states[{i}].affordances must be a list")

        affordances: list[Affordance] = []
        seen_refs: set[str] = set()

        for j, aff_data in enumerate(affordances_data):
            if not isinstance(aff_data, dict):
                raise NavMapError(f"states[{i}].affordances[{j}] must be an object")

            _check_keys(aff_data, _AFFORDANCE_KEYS, f"states[{i}].affordances[{j}]")

            aff_ref = aff_data.get("ref")
            if not isinstance(aff_ref, str) or not aff_ref.strip():
                raise NavMapError(
                    f"states[{i}].affordances[{j}].ref must be a non-empty string"
                )

            # Check for duplicate refs in this state
            if aff_ref in seen_refs:
                raise NavMapError(
                    f"state {state_id!r}: duplicate affordance ref {aff_ref!r}"
                )
            seen_refs.add(aff_ref)

            # At least one of role, name, or css
            aff_role = aff_data.get("role")
            aff_name = aff_data.get("name")
            aff_css = aff_data.get("css")

            if not (
                (isinstance(aff_role, str) and aff_role.strip())
                or (isinstance(aff_name, str) and aff_name.strip())
                or (isinstance(aff_css, str) and aff_css.strip())
            ):
                raise NavMapError(
                    f"states[{i}].affordances[{j}] must have at least one of: "
                    "role, name, or css"
                )

            aff_provenance = _validate_provenance(
                aff_data.get("provenance", "extracted"),
                f"states[{i}].affordances[{j}].provenance",
            )

            affordances.append(
                Affordance(
                    ref=aff_ref,
                    role=aff_role if isinstance(aff_role, str) and aff_role.strip() else None,
                    name=aff_name if isinstance(aff_name, str) and aff_name.strip() else None,
                    css=aff_css if isinstance(aff_css, str) and aff_css.strip() else None,
                    provenance=aff_provenance,
                )
            )

        state_provenance = _validate_provenance(
            state_data.get("provenance", "extracted"),
            f"states[{i}].provenance",
        )

        # Parse fragment from URL
        fragment = _parse_fragment(url, f"states[{i}].url")

        state = State(
            id=state_id,
            url=url,
            landmarks=tuple(landmarks),
            affordances=tuple(affordances),
            fragment=fragment,
            provenance=state_provenance,
        )
        states.append(state)
        states_by_id[state_id] = state

    # Transitions (optional)
    transitions_data = data.get("transitions", [])
    if not isinstance(transitions_data, list):
        raise NavMapError("transitions must be a list")

    transitions: list[Transition] = []
    seen_transitions: set[tuple[str, str]] = set()  # (from_state, canonical-do-json)
    for i, tr_data in enumerate(transitions_data):
        if not isinstance(tr_data, dict):
            raise NavMapError(f"transitions[{i}] must be an object")

        _check_keys(tr_data, _TRANSITION_KEYS, f"transitions[{i}]")

        from_id = tr_data.get("from")
        if not isinstance(from_id, str) or not from_id.strip():
            raise NavMapError(f"transitions[{i}].from must be a non-empty string")

        if from_id not in states_by_id:
            raise NavMapError(f"transitions[{i}].from references unknown state {from_id!r}")

        to_id = tr_data.get("to")
        if not isinstance(to_id, str) or not to_id.strip():
            raise NavMapError(f"transitions[{i}].to must be a non-empty string")

        if to_id not in states_by_id:
            raise NavMapError(f"transitions[{i}].to references unknown state {to_id!r}")

        do = tr_data.get("do")
        if not isinstance(do, dict):
            raise NavMapError(f"transitions[{i}].do must be an object")
        # Exactly one action — a do carrying both click and fill parsed and then silently
        # dropped the fill at runtime (re-attack residual, 2026-07-19). Refuse, don't guess.
        action_keys = set(do) & {"click", "fill"}
        if len(action_keys) != 1 or set(do) - {"click", "fill"}:
            raise NavMapError(
                f"transitions[{i}].do must have exactly one action key (click OR fill)"
            )

        # Validate action shape and refs
        if "click" in do:
            ref = do["click"]
            if not isinstance(ref, str):
                raise NavMapError(
                    f"transitions[{i}].do.click must be a string (affordance ref)"
                )
            from_state = states_by_id[from_id]
            ref_set = {aff.ref for aff in from_state.affordances}
            if ref not in ref_set:
                raise NavMapError(
                    f"transitions[{i}].do.click references unknown affordance {ref!r} "
                    f"in state {from_id!r}"
                )
        elif "fill" in do:
            fill = do["fill"]
            if not isinstance(fill, list) or len(fill) != 2:
                raise NavMapError(
                    f"transitions[{i}].do.fill must be a [ref, text] pair"
                )
            ref, text = fill
            if not isinstance(ref, str) or not isinstance(text, str):
                raise NavMapError(
                    f"transitions[{i}].do.fill must be [ref: str, text: str]"
                )
            from_state = states_by_id[from_id]
            ref_set = {aff.ref for aff in from_state.affordances}
            if ref not in ref_set:
                raise NavMapError(
                    f"transitions[{i}].do.fill references unknown affordance {ref!r} "
                    f"in state {from_id!r}"
                )
        else:
            raise NavMapError(
                f"transitions[{i}].do must have either 'click' or 'fill' key"
            )

        # Check for duplicate (from_state, do) pair using canonical JSON
        canonical_do = json.dumps(do, sort_keys=True, separators=(",", ":"))
        transition_key = (from_id, canonical_do)
        if transition_key in seen_transitions:
            raise NavMapError(
                f"transitions[{i}] duplicate: from_state={from_id!r}, "
                f"do={do} (already declared)"
            )
        seen_transitions.add(transition_key)

        transitions.append(
            Transition(from_state=from_id, do=do, to_state=to_id)
        )

    # Flows (optional)
    flows_data = data.get("flows", [])
    if not isinstance(flows_data, list):
        raise NavMapError("flows must be a list")

    flows: list[Flow] = []
    for i, flow_data in enumerate(flows_data):
        if not isinstance(flow_data, dict):
            raise NavMapError(f"flows[{i}] must be an object")

        _check_keys(flow_data, _FLOW_KEYS, f"flows[{i}]")

        flow_name = flow_data.get("name")
        if not isinstance(flow_name, str) or not flow_name.strip():
            raise NavMapError(f"flows[{i}].name must be a non-empty string")

        path = flow_data.get("path")
        if not isinstance(path, list):
            raise NavMapError(f"flows[{i}].path must be a list")

        for j, state_id in enumerate(path):
            if not isinstance(state_id, str):
                raise NavMapError(f"flows[{i}].path[{j}] must be a string (state id)")
            if state_id not in states_by_id:
                raise NavMapError(
                    f"flows[{i}] ({flow_name!r}).path references unknown state {state_id!r}"
                )

        flows.append(Flow(name=flow_name, path=tuple(path)))

    return NavMap(
        app=app,
        states=tuple(states),
        transitions=tuple(transitions),
        flows=tuple(flows),
        raw=data if isinstance(data, dict) else {},
        entry_url=entry_url,
        entry_actions=entry_actions,
    )


def load(path: Path, *, draft: bool = False) -> NavMap:
    """Load and parse a navigation map JSON file.

    ``draft=True`` is the crawler's lenient mode (see :func:`parse_navmap`).

    Raises NavMapError if:
    - File does not exist
    - JSON is invalid
    - Map is structurally invalid
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NavMapError(f"map file not found: {path}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NavMapError(f"invalid JSON in map file {path}: {exc}") from exc

    return parse_navmap(data, draft=draft)
