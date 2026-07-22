"""FakeDriver: in-memory test double for Driver protocol (browser-free testing).

Ship in the package so users can test their own navigation maps without a browser.
Used throughout the nav module tests and available for integration tests via
``from cartogate.nav.testing import FakeDriver``.
"""

from __future__ import annotations

import re
from pathlib import Path

from cartogate.nav.driver import DriverError, Target, Wait  # noqa: F401

TargetKey = str  # "role:name" or "css:selector"


def key_of(target: Target) -> TargetKey:
    """Convert a Target to a key for use in pages/wiring dicts.

    Priority: role+name first (semantically stable), then css fallback.
    """
    if target.role and target.name:
        return f"{target.role}:{target.name}"
    if target.css:
        return f"css:{target.css}"
    raise ValueError(f"Target must have role+name or css: {target!r}")


class FakeDriver:
    """In-memory navigation driver for testing.

    Simulates a website with a fixed set of pages (URLs with visible targets)
    and wiring (click/fill transitions between pages).

    Args:
        pages: dict mapping URL -> set of visible TargetKeys on that page.
        wiring: dict mapping (URL, TargetKey) -> destination URL for click actions.
        checked: optional dict mapping URL -> set of TargetKeys that are checked on that page.
    """

    def __init__(
        self,
        pages: dict[str, set[TargetKey]],
        wiring: dict[tuple[str, TargetKey], str],
        checked: dict[str, set[TargetKey]] | None = None,
        base_url: str = "http://localhost",
        inventory: dict[str, dict[str, list[dict[str, str]]]] | None = None,
        redirects: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.wiring = wiring
        self.checked = checked or {}
        self.inventory = inventory or {}
        self.base_url = base_url
        # Requested-URL -> landed-URL: models a server 3xx (an SSO/auth
        # redirect can land the browser on a DIFFERENT, possibly off-origin,
        # url). Lets tests express the redirect case a real driver hits but
        # that verbatim ``_current_url = url`` cannot.
        self.redirects = redirects or {}
        self._current_url = ""
        self.actions: list[str] = []

    def navigate(self, url: str) -> None:
        """Navigate to the given URL — map-relative urls resolve against ``base_url``.

        A configured redirect lands the browser on a different url than the one
        requested (as a real 3xx would). Raises DriverError if the LANDED URL is
        not in the pages dict.
        """
        if url.startswith("/"):  # map-relative: resolve like a real driver would
            url = f"{self.base_url}{url}"
        landed = self.redirects.get(url, url)
        if landed not in self.pages:
            raise DriverError(f"navigate to unknown URL {landed!r}")
        self._current_url = landed
        self.actions.append(
            f"navigate: {url} -> {landed}" if landed != url else f"navigate: {url}"
        )

    def current_url(self) -> str:
        """Return the current page URL."""
        return self._current_url

    def click(self, target: Target) -> None:
        """Click on the target element.

        Follows the wiring dict to navigate to the destination URL.
        Raises DriverError if:
          - Target is not visible on the current page
          - No wiring exists for this (url, target) pair
        """
        key = key_of(target)
        if key not in self.pages[self._current_url]:
            raise DriverError(
                f"click: target {key!r} not visible on {self._current_url!r}"
            )
        edge = (self._current_url, key)
        if edge not in self.wiring:
            raise DriverError(
                f"click: no wiring from {self._current_url!r} on {key!r}"
            )
        dest = self.wiring[edge]
        self._current_url = dest
        self.actions.append(f"click: {key} -> {dest}")

    def fill(self, target: Target, text: str) -> None:
        """Fill text into the target element.

        Records the action. Raises DriverError if target is not visible.
        """
        key = key_of(target)
        if key not in self.pages[self._current_url]:
            raise DriverError(
                f"fill: target {key!r} not visible on {self._current_url!r}"
            )
        self.actions.append(f"fill: {key} = {text!r}")

    def wait_for(self, wait: Wait) -> None:
        """Wait for a condition.

        For FakeDriver (no actual waiting), just check:
          - If url_matches: check against current URL
          - If target_visible: check is_visible()
        Raises DriverError if condition is not met.
        """
        if wait.url_matches is not None:
            pattern = wait.url_matches
            if not re.search(pattern, self._current_url):
                raise DriverError(
                    f"wait_for url_matches {pattern!r}: current URL {self._current_url!r}"
                )
        elif wait.target_visible is not None:
            if not self.is_visible(wait.target_visible):
                raise DriverError(
                    f"wait_for target_visible: {key_of(wait.target_visible)!r} "
                    f"not visible on {self._current_url!r}"
                )

    def is_visible(self, target: Target) -> bool:
        """Check if the target element is currently visible.

        Returns False if target is not in the current page's set.
        """
        if not self._current_url:
            return False
        key = key_of(target)
        return key in self.pages.get(self._current_url, set())

    def is_checked(self, target: Target) -> bool:
        """Check if the target element is in checked state.

        Returns True if the target is in the checked set for the current page.
        """
        if not self._current_url:
            return False
        key = key_of(target)
        return key in self.checked.get(self._current_url, set())

    def page_inventory(self) -> dict[str, list[dict[str, str]]]:
        """Landmark/affordance candidates for the current page (crawler eyes).

        Configured per-URL via the ``inventory`` constructor arg; empty when
        unconfigured — the crawler proposes nothing it cannot see.
        """
        entry = self.inventory.get(self._current_url, {})
        return {
            "landmarks": list(entry.get("landmarks", [])),
            "affordances": list(entry.get("affordances", [])),
        }

    def block_mutating_requests(self) -> None:
        """No-op: FakeDriver has no network, so nothing to guard (the engine's
        capability check for discovery is satisfied — see nav.discover)."""

    def screenshot(self, path: Path) -> None:
        """Take a screenshot and save to path.

        For FakeDriver, writes a stub PNG-like byte string.
        """
        # Stub PNG header (simplest valid PNG is just a 1x1 transparent pixel)
        stub = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        Path(path).write_bytes(stub)
        self.actions.append(f"screenshot: {path}")
