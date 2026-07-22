"""SeleniumDriver (Stage 3) — browser-free tests: guard, locator mapping, resolution.

The second adapter exists to prove the Driver seam honest (design spec §3b).
Selenium has no accessibility locator, so role+name targets resolve through a
PURE role→XPath mapping (unit-tested here); css fallbacks are preferred when
present. Live conformance runs in the E2E suite against the same viz flow as
PlaywrightDriver.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from cartogate.nav.driver import Target
from cartogate.nav.schema import NavMapError
from cartogate.nav.selenium_driver import _locator_candidates


class TestLocatorMapping:
    def test_role_name_is_primary_even_when_css_is_declared(self) -> None:
        # The Driver contract everywhere (Target docstring, FakeDriver.key_of,
        # PlaywrightDriver): role+name PRIMARY, css FALLBACK. Inspector
        # Critical: this adapter initially inverted it, and the old test name
        # ("css_fallback_is_preferred") laundered the inversion.
        candidates = _locator_candidates(
            Target(role="button", name="New", css="[data-testid=new]")
        )
        assert candidates[0][0] == "xpath" and "New" in candidates[0][1]
        # The declared css is the RUNTIME fallback — second, never dropped
        # (E2E root cause: label-named radios are invisible to the XPath
        # mapping; without the runtime fallback, where() goes LOST and a
        # fragment-only direct-nav dead-ends on apps with no hashchange).
        assert candidates[1] == ("css selector", "[data-testid=new]")

    def test_css_is_used_when_role_name_is_absent(self) -> None:
        candidates = _locator_candidates(
            Target(role=None, name=None, css="[data-testid=new]")
        )
        assert candidates == [("css selector", "[data-testid=new]")]

    def test_xpath_literal_double_quote_only_name(self) -> None:
        ((strategy, value),) = _locator_candidates(Target(role="button", name='say "hi"'))
        assert strategy == "xpath"
        assert "'say \"hi\"'" in value  # single-quoted literal, no concat needed

    def test_heading_role_maps_to_hN_xpath(self) -> None:
        ((strategy, value),) = _locator_candidates(
            Target(role="heading", name="Invoices")
        )
        assert strategy == "xpath"
        assert "h1" in value and "h6" in value
        assert "Invoices" in value

    def test_link_button_radio_roles_map(self) -> None:
        for role, needle in (("link", "//a"), ("button", "//button"), ("radio", "radio")):
            ((strategy, value),) = _locator_candidates(Target(role=role, name="Go"))
            assert strategy == "xpath"
            assert needle in value

    def test_unknown_role_falls_back_to_accessible_name_probe(self) -> None:
        ((strategy, value),) = _locator_candidates(Target(role="tab", name="Settings"))
        assert strategy == "xpath"
        assert "Settings" in value

    def test_quote_in_name_never_breaks_the_xpath(self) -> None:
        # XPath string literals cannot escape quotes — concat() is required for
        # names containing both quote kinds; a naive f-string injects.
        ((strategy, value),) = _locator_candidates(
            Target(role="button", name='Say "hi" y\'all')
        )
        assert strategy == "xpath"
        assert "concat(" in value

    def test_target_without_role_name_or_css_is_refused(self) -> None:
        with pytest.raises(NavMapError, match="css"):
            _locator_candidates(Target(role=None, name=None, css=None))


class TestSeleniumGuard:
    def test_missing_selenium_yields_actionable_error(self) -> None:
        # Simulate absence regardless of environment: block the import in a
        # subprocess and construct the driver.
        code = (
            "import sys; sys.modules['selenium'] = None\n"
            "from cartogate.nav.selenium_driver import SeleniumDriver\n"
            "try:\n"
            "    SeleniumDriver(base_url='http://localhost')\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, str(exc))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert "nav-selenium" in result.stdout  # names the extra to install


class TestSharedUrlResolution:
    def test_selenium_uses_the_same_resolve_semantics(self) -> None:
        from cartogate.nav.driver import resolve_url

        assert (
            resolve_url("http://localhost:8000", "/viz.html#v=x")
            == "http://localhost:8000/viz.html#v=x"
        )
        assert resolve_url("http://a:1", "http://b:2/full") == "http://b:2/full"


class TestSameDocumentReloadPredicate:
    def test_target_fragment_same_path_needs_reload(self) -> None:
        from cartogate.nav.driver import needs_reload_after_fragment_nav

        assert needs_reload_after_fragment_nav(
            "http://l:1/viz.html", "http://l:1/viz.html#v=globe"
        )

    def test_current_fragment_bare_target_same_path_needs_reload(self) -> None:
        # fragment -> no-fragment at the same path is ALSO same-document
        # (review Medium: the original predicate only checked the target).
        from cartogate.nav.driver import needs_reload_after_fragment_nav

        assert needs_reload_after_fragment_nav(
            "http://l:1/viz.html#v=globe", "http://l:1/viz.html"
        )

    def test_cross_page_and_fresh_browser_do_not_reload(self) -> None:
        from cartogate.nav.driver import needs_reload_after_fragment_nav

        assert not needs_reload_after_fragment_nav(
            "http://l:1/other.html", "http://l:1/viz.html#v=globe"
        )
        assert not needs_reload_after_fragment_nav(
            "about:blank", "http://l:1/viz.html#v=globe"
        )
        assert not needs_reload_after_fragment_nav(
            "http://l:1/viz.html", "http://l:1/viz.html"
        )
