"""nav crawl --discover: bounded frontier discovery (Stage 3; design 2026-07-20).

Proposes NEW states/transitions beyond the declared map by driving a real
browser through navigation-semantic affordances. Five controls make it safe:

1. Non-GET abort (load-bearing, mechanical) — the driver kills any POST/PUT/
   DELETE/PATCH in flight, so the app's server never sees a mutating request.
   PlaywrightDriver-only (route interception); Fake no-ops (no network).
2. HARD loopback refusal — discovery only ever runs against loopback origins;
   there is NO override flag (user decision 2026-07-20).
3. Navigation-semantic clicks only — the inventory surfaces link/button/radio/
   tab/menuitem; never fill, never form submission.
4. Explicit budgets — states/depth/actions/seconds, every hit reported.
5. Same-origin only — off-origin links are recorded dead-ends, never followed.

State equivalence rides the code graph: discovered URLs collapse onto the seed
map's route patterns (``/items/7`` -> declared ``/items/:id``) — no DOM diffing.
Output is quarantined in ``<map>.proposed.json`` (+ a transitions sidecar); the
live map is never written. Merging is the human act of approval.
"""

from __future__ import annotations

import ipaddress
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cartogate.nav.driver import Driver, Target
from cartogate.nav.runtime import _url_matches_pattern
from cartogate.nav.schema import NavMapError, load

_NAV_ROLES = frozenset({"link", "button", "radio", "tab", "menuitem"})


@dataclass
class DiscoveryBudget:
    """Explicit bounds — hitting any one stops discovery and is reported."""

    max_states: int = 30
    max_depth: int = 5
    max_actions: int = 200
    max_seconds: float = 120.0


@dataclass
class DiscoveryReport:
    """What a discovery run found — every bound and dead-end named."""

    proposed_states: list[str] = field(default_factory=list)
    transitions: list[tuple[str, str]] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)
    budget_hits: list[str] = field(default_factory=list)
    actions: int = 0
    proposed_path: Path | None = None


def _should_abort_method(method: str) -> bool:
    """Whether a request method is mutating (everything but GET/HEAD)."""
    return method.upper() not in ("GET", "HEAD")


def _is_loopback(url: str) -> bool:
    """Whether ``url``'s host is a loopback address.

    Fails closed: only ``localhost``, the whole IPv4 ``127.0.0.0/8`` block, and
    IPv6 loopback (``::1`` in any spelling) pass. Widened from an exact set so
    common local binds — ``0.0.0.0`` and ``127.0.0.x`` siblings — are not a
    no-override refusal cliff (review 2026-07-22). ``urlparse().hostname`` is
    the true host (userinfo like ``127.0.0.1@evil.com`` resolves to ``evil.com``
    and is refused).
    """
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        # 0.0.0.0 binds all interfaces incl. loopback in dev; treat as local.
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr == ipaddress.ip_address("0.0.0.0")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return (parsed.scheme, parsed.hostname or "", parsed.port)


def _state_id_for_path(path: str) -> str:
    """Deterministic proposed-state id from a url path (navmap_cli semantics)."""
    stripped = path.strip("/")
    if not stripped:
        return "root"
    return ".".join(seg.lstrip(":") for seg in stripped.split("/"))


class _StateResolver:
    """Collapses observed URLs onto seed patterns, minting stable new ids."""

    def __init__(self, seed_states: list[Any]) -> None:
        # (state_id, path_pattern, param_count) most-specific first.
        self._patterns = sorted(
            (
                (s.id, s.url.split("#")[0], s.url.split("#")[0].count("/:"))
                for s in seed_states
            ),
            key=lambda t: (t[2], t[0]),
        )
        self._seed_ids = {s.id for s in seed_states}
        self._new_by_path: dict[str, str] = {}
        self._used_ids: set[str] = set(self._seed_ids)

    def resolve(self, url: str) -> tuple[str, bool]:
        """(state_id, is_new) for an observed URL."""
        path = urlparse(url).path or "/"
        # Normalize trailing slashes so /docs and /docs/ are ONE state (root
        # stays "/") — otherwise the second spelling mints a spurious docs~2.
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        for state_id, pattern, _ in self._patterns:
            if _url_matches_pattern(path, pattern):
                return state_id, False
        if path in self._new_by_path:
            return self._new_by_path[path], False
        base = _state_id_for_path(path)
        candidate, n = base, 2
        while candidate in self._used_ids:
            candidate, n = f"{base}~{n}", n + 1
        self._used_ids.add(candidate)
        self._new_by_path[path] = candidate
        return candidate, True

    def is_seed(self, state_id: str) -> bool:
        return state_id in self._seed_ids

    def seed_ids(self) -> set[str]:
        return set(self._seed_ids)


def crawl_discover(
    map_path: Path,
    driver: Driver,
    *,
    base_url: str,
    budget: DiscoveryBudget,
    clock: Callable[[], float] = time.monotonic,
) -> DiscoveryReport:
    """Discover new states/transitions from the seed map; quarantine proposals."""
    if not _is_loopback(base_url):
        raise NavMapError(
            f"discovery refuses non-loopback origin {base_url!r} — discovery "
            "drives a real browser through an app and only ever runs against "
            "loopback (localhost/127.0.0.1/::1). There is no override."
        )

    guard = getattr(driver, "block_mutating_requests", None)
    if guard is None:
        raise NavMapError(
            "discovery needs a driver that can block mutating requests — use "
            "PlaywrightDriver (cartogate[nav]). Selenium lacks clean request "
            "interception; verify+propose mode works on both drivers."
        )
    guard()

    navmap = load(map_path, draft=True)
    raw = json.loads(map_path.read_text(encoding="utf-8"))
    resolver = _StateResolver(list(navmap.states))
    report = DiscoveryReport()

    base_origin = _origin(base_url)
    transitions: set[tuple[str, str]] = set()
    dead_ends: set[str] = set()
    budget_hits: set[str] = set()
    proposed: dict[str, dict[str, Any]] = {}
    committed_new = 0
    started = clock()

    # Seed the frontier from param-free existing states (paramful patterns are
    # unnavigable; their concrete instances are reached by collapse).
    frontier: deque[tuple[str, int]] = deque()
    for state in sorted(navmap.states, key=lambda s: s.id):
        pattern = state.url.split("#")[0]
        if ":" not in pattern:
            frontier.append((base_url.rstrip("/") + state.url, 0))
    visited_urls: set[str] = set()

    # max_actions/max_seconds are GLOBAL budgets — hitting one stops the whole
    # crawl (an explicit flag, not a for/else/break — review High: the terse
    # form abandoned the frontier confusingly). max_depth/max_states are
    # PER-EDGE decisions that only skip enqueuing.
    stop = False
    while frontier and not stop:
        if clock() - started >= budget.max_seconds:
            budget_hits.add("max_seconds")
            break
        url, depth = frontier.popleft()
        if url in visited_urls:
            continue
        visited_urls.add(url)

        driver.navigate(url)
        landed = driver.current_url()
        if _origin(landed) != base_origin:
            # A seed/enqueued URL that 3xx-redirects off-origin (SSO, external
            # auth) lands the browser on a foreign origin. The loopback promise
            # is about the LANDED origin, not just base_url: never inventory,
            # propose, or click a foreign page as a state of THIS app (release
            # blocker 2026-07-22). The mutating-request guard still holds, but
            # honesty of provenance and the same-origin invariant do not.
            dead_ends.add(landed)
            continue
        from_id, _ = resolver.resolve(landed)
        # Propose on VISIT (not first-sight): a state discovered as a transition
        # target earlier is already cached in the resolver, so key proposing on
        # "non-seed and not yet proposed", not the resolve first-sight flag.
        if not resolver.is_seed(from_id) and from_id not in proposed:
            proposed[from_id] = _propose_state(driver, from_id)

        affordances = [
            aff
            for aff in driver.page_inventory().get("affordances", [])
            if aff.get("role") in _NAV_ROLES and aff.get("name")
        ]
        for aff in affordances:
            if report.actions >= budget.max_actions:
                budget_hits.add("max_actions")
                stop = True
                break
            if clock() - started >= budget.max_seconds:
                budget_hits.add("max_seconds")
                stop = True
                break
            # Re-seat on the source page before each click (a click navigates).
            driver.navigate(url)
            if _origin(driver.current_url()) != base_origin:
                # The source url now redirects off-origin (defensive — the pop
                # guard already caught the deterministic case). Abandon this
                # page's remaining affordances rather than click a foreign page.
                dead_ends.add(driver.current_url())
                break
            try:
                driver.click(Target(role=aff["role"], name=aff["name"]))
            except Exception:  # noqa: BLE001 — a dead affordance is not a crash
                continue
            report.actions += 1
            dest = driver.current_url()
            if _origin(dest) != base_origin:
                dead_ends.add(dest)
                continue
            to_id, to_new = resolver.resolve(dest)
            transitions.add((from_id, to_id))
            if dest in visited_urls:
                continue
            if depth + 1 > budget.max_depth:
                budget_hits.add("max_depth")
                continue
            if to_new:
                if committed_new >= budget.max_states:
                    budget_hits.add("max_states")
                    continue
                committed_new += 1
            frontier.append((dest, depth + 1))

    # Output is self-consistent regardless of where a budget stopped us: a
    # transition to a state past the explored boundary (discovered but never
    # proposed) is dropped — we can't responsibly suggest wiring an edge to a
    # state we didn't explore (review High: dangling transitions).
    known_ids = resolver.seed_ids() | set(proposed)
    report.proposed_states = sorted(proposed)
    report.transitions = sorted(
        (a, b) for a, b in transitions if a in known_ids and b in known_ids
    )
    report.dead_ends = sorted(dead_ends)
    report.budget_hits = sorted(budget_hits)
    report.proposed_path = _write_proposals(map_path, raw, navmap, proposed)
    _write_transition_sidecar(map_path, report.transitions)
    return report


def _propose_state(driver: Driver, state_id: str) -> dict[str, Any]:
    """A proposed state from the current page's inventory (crawled provenance)."""
    inv = driver.page_inventory()
    path = urlparse(driver.current_url()).path or "/"
    return {
        "id": state_id,
        "url": path,
        "landmarks": [
            {**lm, "provenance": "crawled"} for lm in inv.get("landmarks", [])
        ],
        "affordances": [
            {"ref": f"c{i}", **aff, "provenance": "crawled"}
            for i, aff in enumerate(inv.get("affordances", []), start=1)
        ],
    }


def _echo_seed_state(state: Any) -> dict[str, Any]:
    return {
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


def _write_proposals(
    map_path: Path, raw: dict[str, Any], navmap: Any, proposed: dict[str, dict[str, Any]]
) -> Path:
    states = [_echo_seed_state(s) for s in navmap.states]
    states.extend(proposed[k] for k in sorted(proposed))
    doc = {
        "comment": (
            "Discovered proposals — review, edit, and merge into the map "
            "yourself; discovered states are runtime-observed (crawled "
            "provenance) and never authoritative."
        ),
        "version": raw.get("version", 1),
        "app": raw.get("app", ""),
        "states": states,
        "transitions": [],
        "flows": raw.get("flows", []),
    }
    out = map_path.with_suffix(".proposed.json")
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _write_transition_sidecar(
    map_path: Path, transitions: list[tuple[str, str]]
) -> None:
    sidecar = map_path.with_suffix(".proposed.transitions.json")
    if not transitions:
        # Remove a stale sidecar from a prior run: leaving last run's edges
        # paired with this run's fresh proposals is the cross-run dangling-edge
        # hazard (review 2026-07-22).
        sidecar.unlink(missing_ok=True)
        return
    sidecar.write_text(
        json.dumps(
            {
                "comment": (
                    "Discovered transition candidates. Wire each into the map "
                    "as a transition once its source state declares the "
                    "affordance to click."
                ),
                "transitions": [{"from": a, "to": b} for a, b in transitions],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
