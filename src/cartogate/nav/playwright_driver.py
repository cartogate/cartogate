"""PlaywrightDriver: browser automation via Playwright (lazy-imported, [nav] extra).

All playwright imports are inside functions/methods so the module can be imported
and checked for availability without requiring the playwright package. This enables
the CLI to provide an actionable error message when [nav] is not installed.
"""

from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path
from typing import Any

from cartogate.nav.driver import (
    SCREENSHOT_SETTLE_MS,  # noqa: F401 — re-export (test/import compat)
    DriverError,
    Target,
    Wait,
    needs_reload_after_fragment_nav,
)
from cartogate.nav.driver import resolve_url as _resolve
from cartogate.nav.schema import NavMapError


def require_playwright() -> None:
    """Raise NavMapError if playwright is not installed.

    Call this at the start of any function that needs playwright.
    """
    if importlib.util.find_spec("playwright") is None:
        raise NavMapError(
            "cartogate nav needs the [nav] extra — pip install 'cartogate[nav]'\n"
            "  pipx:  pipx inject cartogate 'playwright>=1.49'\n"
            "  pip:   pip install 'cartogate[nav]'\n"
            "(The gate and the rest of cartogate work fine without it.)"
        )


#: Shared DOM inventory script (also used by SeleniumDriver via execute_script —
#: ONE definition so both drivers see identical candidates).
_INVENTORY_JS = """() => {
  const inv = {landmarks: [], affordances: []};
  for (const h of document.querySelectorAll("h1,h2,h3,h4,h5,h6")) {
    const name = h.innerText.trim();
    if (name) inv.landmarks.push({role: "heading", name});
  }
  for (const r of document.querySelectorAll("input[type=radio]:checked")) {
    const label = r.closest("label");
    const name = (r.getAttribute("aria-label") || (label ? label.innerText.trim() : "")).trim();
    if (name) inv.landmarks.push({role: "radio", name, checked: true});
  }
  const cssFor = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const tid = el.getAttribute("data-testid");
    if (tid) return `[data-testid="${CSS.escape(tid)}"]`;
    if (el.tagName === "INPUT" && el.name && el.value)
      return `input[name="${CSS.escape(el.name)}"][value="${CSS.escape(el.value)}"]`;
    return null;
  };
  const seen = new Set();
  const SEL = "a[href], button, input[type=radio], [role=tab], [role=menuitem]";
  for (const el of document.querySelectorAll(SEL)) {
    const label = el.closest("label");
    // Accessible-name precedence: aria-label, then LABEL text, then content,
    // then value (a form control's value is a last resort — the viz's radios
    // carry view KEYS as values but human names in their labels).
    const name = (el.getAttribute("aria-label") || (label ? label.innerText : "")
      || el.innerText || el.value || "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const role = el.matches("a[href]") ? "link"
      : el.matches("input[type=radio]") ? "radio"
      : el.matches("[role=tab]") ? "tab"
      : el.matches("[role=menuitem]") ? "menuitem" : "button";
    const entry = {role, name};
    const css = cssFor(el);
    if (css) entry.css = css;
    inv.affordances.push(entry);
  }
  return inv;
}"""


class PlaywrightDriver:
    """Browser automation via Playwright sync API.

    Constructor: PlaywrightDriver(base_url, headless=True)
    - base_url: base URL for the browser (e.g., "http://localhost:3000")
    - headless: run in headless mode (no visible browser window)

    Implements the Driver protocol: navigate, current_url, click, fill, wait_for,
    is_visible, screenshot.

    Selector resolution (v1, dual-selector):
      - If role and name are present: use get_by_role(role, name=name)
      - Else: use locator(css)
      This gives priority to semantically stable ARIA selectors, with CSS fallback.
    """

    def __init__(self, base_url: str, headless: bool = True) -> None:
        """Initialize PlaywrightDriver.

        Args:
            base_url: Base URL (e.g., "http://localhost:3000")
            headless: Run in headless mode (default True)

        Raises:
            NavMapError if playwright is not installed.
        """
        require_playwright()
        self.base_url = base_url
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self.aborted_requests: list[str] = []  # discovery guard evidence

    def _ensure_page(self) -> Any:
        """Lazy-initialize browser and page."""
        if self._page is not None:
            return self._page

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        return self._page

    def _locator_for_target(self, target: Target) -> Any:
        """Dual-selector: role+name PRIMARY; declared css is the RUNTIME fallback.

        The fallback fires when the primary matches nothing — the SAME trigger
        as SeleniumDriver._locator_candidates, kept symmetric so identical
        maps resolve identically across drivers (review Medium, 2026-07-20:
        trigger asymmetry could make adapters pick different elements).
        """
        page = self._ensure_page()

        if target.role and target.name:
            primary = page.get_by_role(target.role, name=target.name)
            if target.css and primary.count() == 0:
                return page.locator(target.css)
            return primary

        if target.css:
            return page.locator(target.css)

        raise DriverError(f"Target must have role+name or css: {target!r}")

    def navigate(self, url: str) -> None:
        """Navigate to ``url`` — map-relative urls resolve against ``base_url``."""
        require_playwright()
        page = self._ensure_page()
        url = _resolve(self.base_url, url)
        try:
            reload_needed = needs_reload_after_fragment_nav(page.url, url)
            page.goto(url)
            if reload_needed:
                # Same-document fragment nav: force the fresh load the map's
                # direct-navigation semantics promise (see driver.py helper).
                page.reload()
        except Exception as exc:
            raise DriverError(f"navigate to {url!r} failed: {exc}") from exc

    def current_url(self) -> str:
        """Return the current page URL."""
        require_playwright()
        page = self._ensure_page()
        return str(page.url)

    def click(self, target: Target) -> None:
        """Click on the target element."""
        require_playwright()
        try:
            locator = self._locator_for_target(target)
            locator.click()
        except Exception as exc:
            raise DriverError(f"click on {target!r} failed: {exc}") from exc

    def fill(self, target: Target, text: str) -> None:
        """Fill text into the target element."""
        require_playwright()
        try:
            locator = self._locator_for_target(target)
            locator.fill(text)
        except Exception as exc:
            raise DriverError(f"fill {target!r} with {text!r} failed: {exc}") from exc

    def wait_for(self, wait: Wait) -> None:
        """Wait for a condition."""
        require_playwright()
        page = self._ensure_page()

        try:
            if wait.url_matches is not None:
                # Wait for URL to match a pattern
                page.wait_for_url(wait.url_matches, timeout=wait.timeout_s * 1000)
            elif wait.target_visible is not None:
                # Wait for element to be visible
                locator = self._locator_for_target(wait.target_visible)
                locator.wait_for(state="visible", timeout=wait.timeout_s * 1000)
        except Exception as exc:
            raise DriverError(f"wait_for {wait!r} failed: {exc}") from exc

    def is_visible(self, target: Target) -> bool:
        """Check if the target element is visible."""
        require_playwright()
        try:
            locator = self._locator_for_target(target)
            return bool(locator.is_visible())
        except Exception:
            # If locator resolution fails or element doesn't exist, it's not visible
            return False

    def is_checked(self, target: Target) -> bool:
        """Check if the target element is in checked state (radio/checkbox/aria-checked)."""
        require_playwright()
        try:
            locator = self._locator_for_target(target)
            return bool(locator.is_checked())
        except Exception:
            # If locator resolution fails or element doesn't exist, it's not checked
            return False

    def page_inventory(self) -> dict[str, list[dict[str, str]]]:
        """Landmark/affordance candidates in document order (crawler eyes)."""
        require_playwright()
        try:
            result = self._ensure_page().evaluate(_INVENTORY_JS)
            return {
                "landmarks": list(result.get("landmarks", [])),
                "affordances": list(result.get("affordances", [])),
            }
        except Exception as exc:
            raise DriverError(f"page_inventory failed: {exc}") from exc

    def block_mutating_requests(self) -> None:
        """Install the discovery guard: abort every non-GET/HEAD request.

        The load-bearing safety control for frontier discovery — a click that
        would fire a POST/PUT/DELETE/PATCH has its request killed in flight, so
        the app's server never receives a mutating request. Aborted request
        URLs are recorded on ``aborted_requests`` for verification.

        Routed at the BROWSER CONTEXT level, not the page: a target="_blank"
        link or window.open() spawns a new page that a page-scoped handler
        would not cover — the guard must not be bypassable by a popup (review
        High 2026-07-21).
        """
        from cartogate.nav.discover import _should_abort_method

        page = self._ensure_page()

        def _handler(route: Any) -> None:
            if _should_abort_method(route.request.method):
                self.aborted_requests.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        page.context.route("**/*", _handler)

    def screenshot(self, path: Path) -> None:
        """Take a screenshot and save to path.

        Settles before capturing: apps animate between states (the cartogate viz
        morphs views over 620 ms) and an immediate capture races the paint —
        dogfooded in the A/B pilot as blank 4 KiB captures while landmark
        verification passed. Fixed settle + double-rAF flush keeps this
        deterministic for a given app build.
        """
        require_playwright()
        try:
            page = self._ensure_page()
            page.wait_for_timeout(SCREENSHOT_SETTLE_MS)
            page.evaluate(
                "() => new Promise(r =>"
                " requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
            page.screenshot(path=str(path))
        except Exception as exc:
            raise DriverError(f"screenshot to {path!r} failed: {exc}") from exc

    def close(self) -> None:
        """Close the browser and stop Playwright — best-effort and idempotent.

        Each teardown step is isolated so a fault in one still runs the next,
        and the handles are nulled first so a second call is a no-op. A browser
        whose ``close()`` raised used to skip ``playwright.stop()`` and orphan a
        chromium process — the source of the leaked browsers in the A/B rig
        (review 2026-07-22).
        """
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = self._page = None
        if browser is not None:
            with contextlib.suppress(Exception):  # teardown is best-effort
                browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):  # teardown is best-effort
                playwright.stop()
