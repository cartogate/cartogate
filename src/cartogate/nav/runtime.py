"""Navigator runtime: deterministic where/goto/affordances/capture (spec §6).

This module implements the no-exploration guarantee: the runtime never explores the
UI. Navigation follows only declared map edges; URL matching is bounded to avoid
ambiguity. All operations are deterministic (bounded BFS, lexicographic tie-breaks).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cartogate.nav.driver import Driver, Target
from cartogate.nav.schema import Affordance, Landmark, NavMap, Transition


class NavigationError(RuntimeError):
    """Navigation failed — named the hop/selector/state that failed."""


LOST = "lost"

# Bounded settle for landmark verification (default): multi-MB SVG apps under
# load need seconds to parse+render before landmarks exist — E2E flaked at 2 s.
# Success returns the moment landmarks verify; only true refusals pay the bound.
LANDMARK_SETTLE_S = 10.0


def _url_params_count(pattern: str) -> int:
    """Count :param placeholders in a URL pattern (only after /).

    Ignores fragment part (after #) — counts only path params.
    """
    # Split on # to remove fragment before counting
    path_part = pattern.split("#")[0] if "#" in pattern else pattern
    # Only count :word that comes after a / (param placeholder, not literal colon)
    return len(re.findall(r"/:\w+", path_part))


def _url_matches_pattern(url: str, pattern: str) -> bool:
    """Check if url matches the pattern (exact match or with :param segments).

    Treats :word as a param placeholder ONLY when it follows a / directly.
    Literal colons (e.g., /user:admin) are matched exactly, not as wildcards.
    """
    # Convert pattern to regex: /items/:id -> ^/items/[^/]+$
    regex = re.escape(pattern)
    # Only replace /:word (param after /) with [^/]+
    regex = re.sub(r"(?<=/):\w+", "[^/]+", regex)
    regex = f"^{regex}$"
    return bool(re.match(regex, url))


# (search and execution are strictly separated — see Navigator._find_path; review C1)


def _parse_live_fragment(url: str) -> dict[str, str]:
    """Parse the live URL's fragment into key-value pairs — RAW, like the schema side.

    parse_qsl percent-decodes and turns '+' into a space (form-encoding conventions a URL
    fragment does not follow) — declared "a+b" then never matched live "a+b" (review Medium,
    Stage 2A). Both sides now split raw on '&' then the first '='; junk segments without
    '=' are ignored. This also matches how the viz's own restoreHash() JS parses its hash.
    """
    fragment_str = urlparse(url).fragment
    pairs: dict[str, str] = {}
    for segment in fragment_str.split("&"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        if key and value:
            pairs[key] = value
    return pairs


class Navigator:
    """Deterministic navigator: where(), goto(), affordances(), capture()."""

    def __init__(
        self,
        driver: Driver,
        navmap: NavMap,
        max_hops: int = 10,
        settle_s: float = LANDMARK_SETTLE_S,
    ) -> None:
        self.driver = driver
        self.navmap = navmap
        self.max_hops = max_hops
        self.settle_s = settle_s

    def where(self) -> str:
        """Determine current state by URL match + landmark verification.

        Returns:
            State id if a state's URL and all landmarks match, else LOST.

        Tie-break (determinism): most-specific match (fewest param segments,
        then more declared fragment pairs), then lexicographic order.
        """
        candidates = self._url_candidates(self.driver.current_url())

        # Try matches in order; return first where all landmarks verified (visible + checked)
        for state_id, _, _ in candidates:
            state = self.navmap.state(state_id)
            all_match = all(
                self._landmark_verified(lm)
                for lm in state.landmarks
            )
            if all_match:
                return state_id

        return LOST

    def _url_candidates(self, full_url: str) -> list[tuple[str, int, int]]:
        """URL-matching states, most-specific first — the structural half of where().

        Split out so settling can distinguish "URL matches nothing" (LOST is
        final, return fast) from "URL matches but landmarks not yet verified"
        (worth polling — the page may still be rendering).
        """
        path = urlparse(full_url).path or "/"
        live_fragment = _parse_live_fragment(full_url)
        candidates: list[tuple[str, int, int]] = []  # (state_id, params, -frag_len)
        for state in self.navmap.states:
            state_path = state.url.split("#")[0] if "#" in state.url else state.url
            if not _url_matches_pattern(path, state_path):
                continue
            # Subset semantics: every declared fragment pair must match live.
            if state.fragment and not all(
                live_fragment.get(k) == v for k, v in state.fragment
            ):
                continue
            candidates.append(
                (state.id, _url_params_count(state.url), -len(state.fragment))
            )
        # Fewest params first, then most fragment pairs, then alphabetically.
        candidates.sort(key=lambda x: (x[1], x[2], x[0]))
        return candidates

    def _where_settled(self) -> str:
        """where() with the bounded settle — for DECISIONS, not detection.

        A single-shot probe mid-render can transiently miss a landmark; if
        goto() trusts that LOST it falls into the direct-nav fallback, which
        dead-ends on same-document fragment navigation (E2E root cause,
        live-probed 2026-07-20). Structurally-unknown URLs stay instant: when
        nothing matches the URL, no amount of waiting will change it.
        """
        import time

        deadline = time.monotonic() + self.settle_s
        while True:
            if not self._url_candidates(self.driver.current_url()):
                return LOST
            state = self.where()
            if state != LOST or time.monotonic() >= deadline:
                return state
            time.sleep(0.1)

    def goto(self, state_id: str) -> str:
        """Navigate to a state, preferring declared transitions, with fallback to direct URL.

        Args:
            state_id: Target state id.

        Returns:
            The state id (for confirmation).

        Raises:
            NavigationError if the state is unreachable or max_hops exceeded.

        Strategy (Task 3, Stage 2A - prefer-path):
            1. If already at target → return.
            2. If current state is known (not LOST), attempt BFS to find declared path.
               - If path exists → execute it (declared wiring is now always exercised).
            3. Fall back to direct URL navigation ONLY if:
               - current == LOST, OR
               - no path exists from current to target
               - AND target URL has no :param segments (param-free only).
            4. For param-full targets with no path → raise (unreachable).
        """
        state = self.navmap.state(state_id)

        current = self._where_settled()
        if current == state_id:
            return state_id

        # Attempt to find a declared path if current state is known
        path: list[Transition] | None = None
        if current != LOST:
            path = self._find_path_or_none(current, state_id)

        # If path exists, execute it (declared wiring is now always exercised)
        if path is not None:
            for tr in path:
                self._execute_transition(tr, state_id)
                self._verify_landmarks(tr.to_state, state_id, f"at {tr.to_state!r}")
            return state_id

        # Fallback: direct URL navigation (only for param-free targets)
        has_params = _url_params_count(state.url) > 0
        if has_params:
            # Param-full target with no declared path — unreachable
            raise NavigationError(
                f"goto {state_id!r}: state unreachable from {current!r} "
                f"within max_hops ({self.max_hops})"
            )

        # Direct navigation — pass the MAP-RELATIVE url verbatim (fragment included);
        # the DRIVER resolves it against its base_url (origin is deployment config, not
        # map content — hardcoding localhost broke --base-url runs).
        self.driver.navigate(state.url)
        # Verify landmarks
        self._verify_landmarks(state_id, state_id, f"after navigating to {state.url!r}")
        return state_id

    def _find_path(self, start: str, goal: str) -> list[Transition]:
        """Shortest transition path via PURE graph BFS — no driver calls (review C1).

        Deterministic: transitions are considered in declared order, so among equal-length
        paths the earliest-declared wins. Bounded by ``max_hops``.

        Raises NavigationError if no path exists.
        """
        path = self._find_path_or_none(start, goal)
        if path is None:
            raise NavigationError(
                f"goto {goal!r}: state unreachable from {start!r} "
                f"within max_hops ({self.max_hops})"
            )
        return path

    def _find_path_or_none(self, start: str, goal: str) -> list[Transition] | None:
        """Shortest transition path via PURE graph BFS — no driver calls (review C1).

        Deterministic: transitions are considered in declared order, so among equal-length
        paths the earliest-declared wins. Bounded by ``max_hops``.

        Returns:
            Path as list of transitions, or None if no path exists.
        """
        queue: deque[tuple[str, list[Transition]]] = deque([(start, [])])
        visited: set[str] = {start}
        while queue:
            state_id, path = queue.popleft()
            if len(path) >= self.max_hops:
                continue
            for tr in self.navmap.transitions:
                if tr.from_state != state_id or tr.to_state in visited:
                    continue
                new_path = path + [tr]
                if tr.to_state == goal:
                    return new_path
                visited.add(tr.to_state)
                queue.append((tr.to_state, new_path))
        return None

    def _resolve_affordance(self, state_id: str, ref: str, goal: str) -> Target:
        """The Target for ``ref`` declared on ``state_id`` — unknown ref is a map bug."""
        for aff in self.navmap.state(state_id).affordances:
            if aff.ref == ref:
                return Target(role=aff.role, name=aff.name, css=aff.css)
        raise NavigationError(
            f"goto {goal!r}: transition from {state_id!r} references unknown "
            f"affordance {ref!r}"
        )

    def _landmark_verified(self, landmark: Landmark) -> bool:
        """Check if a landmark is verified: visible and checked state matches (if declared).

        Returns:
            True if the landmark is visible and its checked state (if declared) matches.
        """
        target = Target(role=landmark.role, name=landmark.name)
        if not self.driver.is_visible(target):
            return False
        # A declared checked state must match exactly; undeclared means visibility suffices.
        return landmark.checked is None or self.driver.is_checked(target) == landmark.checked

    def _verify_landmarks(self, state_id: str, goal: str, context: str) -> None:
        """Every landmark of ``state_id`` must verify within a bounded settle window.

        Bounded-eventually, not instant: apps set state (e.g. a checked radio
        from a URL fragment) a beat after navigation, and an instant probe
        races the app's own JS — SeleniumDriver surfaced this live in E2E
        (Playwright's pacing merely hid it). The bound keeps determinism: same
        outcome for any driver slower than the app, refusal past the deadline.
        """
        import time

        deadline = time.monotonic() + self.settle_s
        landmarks = self.navmap.state(state_id).landmarks
        while True:
            if all(self._landmark_verified(lm) for lm in landmarks):
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        for landmark in landmarks:
            if not self._landmark_verified(landmark):
                if landmark.checked is not None:
                    raise NavigationError(
                        f"goto {goal!r}: {context}, landmark "
                        f"{landmark.role!r}/{landmark.name!r} "
                        f"(checked={landmark.checked}) not verified"
                    )
                else:
                    raise NavigationError(
                        f"goto {goal!r}: {context}, landmark "
                        f"{landmark.role!r}/{landmark.name!r} not visible"
                    )

    def _execute_transition(self, tr: Transition, goal: str) -> None:
        """Execute ONE resolved hop's declared action (click or fill) — nothing else."""
        if "click" in tr.do:
            ref = tr.do["click"]
            target = self._resolve_affordance(tr.from_state, str(ref), goal)
            try:
                self.driver.click(target)
            except Exception as exc:
                raise NavigationError(
                    f"goto {goal!r}: click action on {ref!r} failed: {exc}"
                ) from exc
        elif "fill" in tr.do:
            ref, text = tr.do["fill"]
            target = self._resolve_affordance(tr.from_state, str(ref), goal)
            try:
                self.driver.fill(target, str(text))
            except Exception as exc:
                raise NavigationError(
                    f"goto {goal!r}: fill action on {ref!r} failed: {exc}"
                ) from exc

    def affordances(self) -> list[Affordance]:
        """Return the current state's declared affordances verbatim.

        These are the affordances as the map declares them — this does NOT
        probe the live page or mark them live/dead (use the driver's
        ``is_visible`` for that). Returns an empty list when LOST.
        """
        current = self.where()
        if current == LOST:
            return []

        return list(self.navmap.state(current).affordances)

    def capture(self, state_id: str, out_dir: Path) -> dict[str, Any]:
        """Go to state, take screenshot, return sealed evidence bundle.

        Also upserts the capture into ``out_dir/report.json`` — the evidence
        manifest is machine-produced, not agent-narrated (pilot finding: a
        subject did every verification right, wrote its own report to the wrong
        directory, and claimed done; Sonnet b-4, 2026-07-19).

        Args:
            state_id: Target state id.
            out_dir: Directory to write screenshot + manifest.

        Returns:
            Dict with keys: state, url, map_hash, image_path, image_blake2b,
            manifest_path.
        """
        # Validate the existing manifest BEFORE any side effect: a refusal must
        # leave zero artifacts, or the error path itself would produce evidence
        # with no manifest entry (inspector High, 2026-07-20).
        existing_captures = self._load_manifest(out_dir)

        # Navigate to the state
        self.goto(state_id)

        # Take screenshot
        url = self.driver.current_url()
        screenshot_name = f"{state_id.replace('.', '_')}.png"
        screenshot_path = out_dir / screenshot_name
        self.driver.screenshot(screenshot_path)

        # Compute hash of screenshot
        image_bytes = screenshot_path.read_bytes()
        image_hash = hashlib.blake2b(image_bytes).hexdigest()

        manifest_path = self._write_manifest(
            out_dir, existing_captures, screenshot_name, url
        )

        # Compute map hash (or get from raw)
        from cartogate.nav.schema import map_hash

        map_hash_val = self.navmap.raw.get("map_hash", map_hash(self.navmap.raw))

        return {
            "state": state_id,
            "url": url,
            "map_hash": map_hash_val,
            "image_path": str(screenshot_path),
            "image_blake2b": image_hash,
            "manifest_path": str(manifest_path),
        }

    def _load_manifest(self, out_dir: Path) -> list[Any]:
        """Read + validate ``out_dir/report.json`` — the fail-fast half.

        Runs before any side effect (navigation, screenshot). A malformed
        existing file is REFUSED, not clobbered — it may be someone else's
        evidence. Entries we don't understand are preserved verbatim.
        """
        manifest_path = out_dir / "report.json"
        if not manifest_path.exists():
            return []
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = raw["captures"]
            if not isinstance(entries, list):
                raise ValueError("'captures' is not a list")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise NavigationError(
                f"capture: existing {manifest_path} is not a valid manifest "
                f"({exc}) — refusing to overwrite it"
            ) from exc
        return entries

    def _write_manifest(
        self, out_dir: Path, captures: list[Any], name: str, url: str
    ) -> Path:
        """Upsert ``{name, url}`` into the pre-validated list and write atomically.

        Order of first appearance is preserved; a re-captured state replaces
        its entry in place.
        """
        manifest_path = out_dir / "report.json"
        entry = {"name": name, "url": url}
        for i, existing in enumerate(captures):
            if isinstance(existing, dict) and existing.get("name") == name:
                captures[i] = entry
                break
        else:
            captures.append(entry)

        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"captures": captures}, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(manifest_path)
        return manifest_path
