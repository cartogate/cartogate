"""SeleniumDriver — the second Driver adapter (design spec §3b, Stage 3).

Ships specifically to prove the Driver seam is honest, not as an afterthought.
Selenium has no accessibility locator (the gap the dual-selector map design
anticipated), so role+name targets resolve through a PURE, documented
role→XPath mapping; css fallbacks are preferred whenever declared. Live
conformance runs in the E2E suite against the same viz flow as
PlaywrightDriver. All selenium imports are lazy; installing the adapter is
``pip install cartogate[nav-selenium]``.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path
from typing import Any

from cartogate.nav.driver import (
    SCREENSHOT_SETTLE_MS,
    DriverError,
    Target,
    Wait,
    needs_reload_after_fragment_nav,
)
from cartogate.nav.driver import resolve_url as _resolve
from cartogate.nav.schema import NavMapError

_WAIT_POLL_S = 0.2


def require_selenium() -> None:
    """Raise an actionable error when the selenium extra is absent."""
    try:
        import selenium  # noqa: F401
    except ImportError as exc:
        raise NavMapError(
            "SeleniumDriver needs the selenium package — install with "
            "`pip install cartogate[nav-selenium]` (or use the default "
            "PlaywrightDriver via cartogate[nav])."
        ) from exc


def _xpath_literal(value: str) -> str:
    """An XPath 1.0 string literal for ``value`` — quote-safe via concat().

    XPath 1.0 cannot escape quotes inside literals; a name containing both
    quote kinds must be assembled with concat() or the expression breaks
    (and a crafted name would otherwise change the query's meaning).
    """
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    joined = ", '\"', ".join(f'"{part}"' for part in parts)
    return f"concat({joined})"


def _locator_candidates(target: Target) -> list[tuple[str, str]]:
    """Ordered ``(strategy, value)`` candidates for Selenium — pure function.

    role+name is PRIMARY (the Driver contract shared by every adapter); the
    declared css is the RUNTIME fallback, tried when the primary locator
    matches nothing. This is the dual-selector design doing its job: the
    XPath approximation of accessible names cannot see label-derived names
    (E2E root cause, 2026-07-20 — where() went LOST on the viz's radios and a
    fragment-only direct-nav dead-ended on an app with no hashchange
    listener). Targets with neither are refused (honesty over guessing).
    """
    if not target.role or not target.name:
        if target.css:
            return [("css selector", target.css)]
        raise NavMapError(
            "SeleniumDriver needs role+name or a css fallback on every target — "
            "declare `css` in the map for targets Selenium must locate."
        )
    candidates = [_role_xpath(target.role, target.name)]
    if target.css:
        candidates.append(("css selector", target.css))
    return candidates


def _role_xpath(role: str, name: str) -> tuple[str, str]:
    """Best-effort XPath for an accessible role+name — the primary candidate."""
    lit = _xpath_literal(name)
    if role == "heading":
        tags = " or ".join(f"self::h{i}" for i in range(1, 7))
        return ("xpath", f"//*[{tags}][normalize-space(.)={lit}]")
    if role == "link":
        return ("xpath", f"//a[normalize-space(.)={lit} or @aria-label={lit}]")
    if role == "button":
        return (
            "xpath",
            f"//button[normalize-space(.)={lit} or @aria-label={lit}]"
            f" | //input[(@type='button' or @type='submit') and @value={lit}]",
        )
    if role == "radio":
        return (
            "xpath",
            f"//input[@type='radio' and (@aria-label={lit} or @value={lit})]"
            f" | //label[normalize-space(.)={lit}]//input[@type='radio']"
            f" | //label[normalize-space(.)={lit}]/preceding-sibling::input[@type='radio'][1]",
        )
    # Best-effort accessible-name probe for remaining roles (tab, menuitem, ...).
    return (
        "xpath",
        f"//*[@role={_xpath_literal(role)} and (normalize-space(.)={lit} or @aria-label={lit})]"
        f" | //*[@aria-label={lit}]",
    )


def _iter_find(driver: Any, candidates: list[tuple[str, str]]) -> Any:
    """First element matched by the candidate list, else None."""
    for strategy, value in candidates:
        try:
            found = driver.find_elements(strategy, value)
        except Exception:  # noqa: BLE001 — a bad locator is just a non-match
            continue
        if found:
            return found[0]
    return None


class SeleniumDriver:
    """Selenium adapter for the Driver protocol (Chrome, headless by default)."""

    def __init__(self, base_url: str = "http://localhost", headless: bool = True) -> None:
        require_selenium()
        self.base_url = base_url
        self.headless = headless
        self._driver: Any = None

    def _ensure(self) -> Any:
        if self._driver is None:
            from selenium import webdriver

            options = webdriver.ChromeOptions()
            if self.headless:
                options.add_argument("--headless=new")
            options.add_argument("--window-size=1280,900")
            self._driver = webdriver.Chrome(options=options)
        return self._driver

    def _find(self, target: Target) -> Any:
        candidates = _locator_candidates(target)
        element = _iter_find(self._ensure(), candidates)
        if element is None:
            tried = "; ".join(f"{how}={what!r}" for how, what in candidates)
            raise DriverError(f"element not found for {target!r} (tried: {tried})")
        return element

    def navigate(self, url: str) -> None:
        """Navigate to ``url`` — map-relative urls resolve against ``base_url``."""
        try:
            driver = self._ensure()
            resolved = _resolve(self.base_url, url)
            reload_needed = needs_reload_after_fragment_nav(
                str(driver.current_url or ""), resolved
            )
            driver.get(resolved)
            if reload_needed:
                # Same-document fragment nav: force the fresh load the map's
                # direct-navigation semantics promise (see driver.py helper).
                driver.refresh()
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"navigate to {url!r} failed: {exc}") from exc

    def current_url(self) -> str:
        """The browser's current URL."""
        return str(self._ensure().current_url)

    def click(self, target: Target) -> None:
        """Click the resolved element."""
        try:
            self._find(target).click()
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"click on {target!r} failed: {exc}") from exc

    def fill(self, target: Target, text: str) -> None:
        """Clear and type into the resolved element."""
        try:
            element = self._find(target)
            element.clear()
            element.send_keys(text)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(f"fill on {target!r} failed: {exc}") from exc

    def wait_for(self, wait: Wait) -> None:
        """Poll for the wait condition (url regex or target visibility).

        ``Wait`` guarantees exactly one condition and ``timeout_s > 0``, so no
        default-timeout fallback or neither-condition branch is needed here.
        """
        timeout = wait.timeout_s
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if wait.url_matches is not None:
                if re.search(wait.url_matches, self.current_url()):
                    return
            elif wait.target_visible is not None and self.is_visible(
                wait.target_visible
            ):
                return
            time.sleep(_WAIT_POLL_S)
        raise DriverError(f"wait_for timed out after {timeout}s: {wait!r}")

    def is_visible(self, target: Target) -> bool:
        """Whether any candidate-resolved element is displayed."""
        try:
            for strategy, value in _locator_candidates(target):
                elements = self._ensure().find_elements(strategy, value)
                if any(e.is_displayed() for e in elements):
                    return True
        except Exception:  # noqa: BLE001 — visibility probes never raise
            return False
        return False

    def is_checked(self, target: Target) -> bool:
        """Whether the resolved element is selected (radio/checkbox state)."""
        try:
            return bool(self._find(target).is_selected())
        except DriverError:
            return False

    def page_inventory(self) -> dict[str, list[dict[str, str]]]:
        """Landmark/affordance candidates in document order (crawler eyes).

        Runs the SAME DOM script as PlaywrightDriver (imported constant) so
        both adapters see identical candidates — conformance by construction.
        """
        from cartogate.nav.playwright_driver import _INVENTORY_JS

        try:
            result = self._ensure().execute_script(f"return ({_INVENTORY_JS})();")
            return {
                "landmarks": list(result.get("landmarks", [])),
                "affordances": list(result.get("affordances", [])),
            }
        except Exception as exc:
            raise DriverError(f"page_inventory failed: {exc}") from exc

    def screenshot(self, path: Path) -> None:
        """Save a screenshot with the same settle + paint-flush as Playwright.

        Parity matters: the settle exists because an immediate capture raced
        the app's paint (blank 4 KiB captures, dogfooded in the A/B pilot).
        """
        try:
            driver = self._ensure()
            time.sleep(SCREENSHOT_SETTLE_MS / 1000)
            driver.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                " requestAnimationFrame(() => requestAnimationFrame(done));"
            )
            driver.save_screenshot(str(path))
        except Exception as exc:
            raise DriverError(f"screenshot to {path!r} failed: {exc}") from exc

    def close(self) -> None:
        """Quit the browser."""
        if self._driver is not None:
            with contextlib.suppress(Exception):  # teardown must never raise
                self._driver.quit()
            self._driver = None
