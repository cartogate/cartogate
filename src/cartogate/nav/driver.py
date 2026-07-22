"""Driver protocol: browser abstraction for navigation tests.

The protocol defines the minimum interface needed for deterministic navigation:
navigate to URLs, locate elements, interact with them, wait for conditions, and
take screenshots. Implementations include FakeDriver (in-memory, for testing) and
PlaywrightDriver (real browser via [nav] extra).

NOTE (v1 narrowing): The spec defines ``query()`` and ``a11y_snapshot()`` methods
for full AX-tree access. These are deferred to Stage 3/MCP. The v1 protocol folds
them into simpler methods:
  - ``query(role, name, css)`` → removed; resolution happens in adapter layer
  - ``a11y_snapshot()`` → removed; use ``is_visible()`` for individual checks
The adapter (PlaywrightDriver) resolves Target internally: tries role+name first
(via getByRole), falls back to css (locator) on resolution failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DriverError(RuntimeError):
    """A driver action failed (element missing, timeout, navigation failed) — names target/url."""


@dataclass(frozen=True)
class Target:
    """What to act on: role+name primary, css fallback (spec §4 dual-selector strategy).

    At least one must be present. The driver adapter resolves using:
      1. role+name first (semantically stable, ARIA-backed)
      2. css fallback (less stable, browser-specific, but useful for tests)
    """

    role: str | None = None
    name: str | None = None
    css: str | None = None


@dataclass(frozen=True)
class Wait:
    """Condition to wait for: exactly one of url_matches (regex pattern) / target_visible.

    The driver waits up to ``timeout_s`` (seconds, must be > 0) for the
    condition to be true. Enforcing "exactly one condition, positive timeout"
    at construction removes two cross-driver divergences: a Wait with neither
    condition was a silent no-op on some drivers, and ``timeout_s=0`` meant
    "use the default" on Selenium but "wait forever" on Playwright (review
    2026-07-22).
    """

    url_matches: str | None = None
    target_visible: Target | None = None
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if (self.url_matches is None) == (self.target_visible is None):
            raise ValueError(
                "Wait needs exactly one of url_matches / target_visible "
                f"(got url_matches={self.url_matches!r}, "
                f"target_visible={self.target_visible!r})"
            )
        if self.timeout_s <= 0:
            raise ValueError(f"Wait.timeout_s must be > 0 (got {self.timeout_s!r})")


class Driver(Protocol):
    """Protocol for browser automation. Implementations: FakeDriver, PlaywrightDriver."""

    def navigate(self, url: str) -> None:
        """Navigate to the given URL. Raises DriverError if navigation fails."""
        ...

    def current_url(self) -> str:
        """Return the current page URL."""
        ...

    def click(self, target: Target) -> None:
        """Click on the target element. Raises DriverError if element not found."""
        ...

    def fill(self, target: Target, text: str) -> None:
        """Fill text into the target element. Raises DriverError if element not found."""
        ...

    def wait_for(self, wait: Wait) -> None:
        """Wait for a condition (url_matches or target_visible). Raises DriverError on timeout."""
        ...

    def is_visible(self, target: Target) -> bool:
        """Check if the target element is currently visible. Returns False if not found."""
        ...

    def is_checked(self, target: Target) -> bool:
        """Check if the target element is in checked state (radio/checkbox/aria-checked).

        Returns True if checked, False if not checked or element not found.
        """
        ...

    def screenshot(self, path: Path) -> None:
        """Take a screenshot and save to path. Raises DriverError on failure."""
        ...

    def page_inventory(self) -> dict[str, list[dict[str, str]]]:
        """Landmark/affordance candidates on the current page (crawler eyes).

        ``{"landmarks": [{role, name[, checked]}], "affordances": [{role,
        name[, css]}]}`` in document order. Headings and checked radios make
        landmark candidates; links/buttons/radios/tabs make affordance
        candidates with a css fallback when derivable. Best-effort — the
        crawler proposes only what the driver can see.
        """
        ...

# Longer than any declared state-transition animation in the mapped app (the
# cartogate viz's morph is 620 ms). Screenshots are evidence, not hot path.
# Shared by every adapter — parity is the point (blank-capture bug, A/B pilot).
SCREENSHOT_SETTLE_MS = 750


def resolve_url(base_url: str, url: str) -> str:
    """Join a map-relative url onto ``base_url``; absolute urls pass through unchanged.

    Drivers, not the Navigator, own origin resolution — the map declares paths
    and fragments, deployment config declares where the app runs. Shared by
    every adapter (moved from playwright_driver when SeleniumDriver landed).
    """
    if url.startswith("/"):
        return f"{base_url.rstrip('/')}{url}"
    return url


def needs_reload_after_fragment_nav(current_url: str, target_url: str) -> bool:
    """True when navigating current→target is same-document (fragment-only).

    Browsers do NOT reload on same-document navigation; an app without a
    hashchange listener silently ignores it (live-probed on the viz,
    2026-07-20: URL changed, radios did not). A map's direct navigation MEANS
    a fresh load, so adapters force a reload in exactly this case. The
    predicate covers a fragment on EITHER side: fragment-to-bare at the same
    path is same-document too (review Medium — the original only checked the
    target).
    """
    from urllib.parse import urlparse

    cur, tgt = urlparse(current_url), urlparse(target_url)
    if not cur.fragment and "#" not in target_url:
        return False  # no fragment on either side: a plain goto already loads
    return (cur.scheme, cur.netloc, cur.path) == (tgt.scheme, tgt.netloc, tgt.path)
