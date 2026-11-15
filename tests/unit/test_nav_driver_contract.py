"""Cross-driver contract fixes (post-0.7.0 punch list) — browser-free.

Three divergences let the SAME map behave differently per driver:
- Wait.url_matches is documented as a REGEX, but PlaywrightDriver passed the
  string to page.wait_for_url, which treats plain strings as GLOBS.
- An ambiguous role+name target errored on Playwright (strict locators) but
  silently clicked the FIRST match on Selenium — the hidden-wrong-element bug.
- is_checked returned False on ANY failure in Playwright but let non-DriverError
  exceptions (e.g. stale element) escape on Selenium.

These tests pin the unified semantics via pure helpers; live conformance stays
in the E2E suite.
"""

from __future__ import annotations

import pytest

from cartogate.nav.driver import DriverError, Target, url_matched
from cartogate.nav.selenium_driver import _find_unique


class TestUrlMatched:
    """The ONE url-wait semantic every adapter (and FakeDriver) imports."""

    def test_regex_search_semantics(self) -> None:
        assert url_matched(r"items/\d+", "http://localhost/items/7")
        assert not url_matched(r"items/\d+", "http://localhost/items/abc")

    def test_plain_substring_still_matches_anywhere(self) -> None:
        # re.search, not fullmatch: existing maps wrote bare substrings.
        assert url_matched("about", "http://localhost/about?x=1")


class _Element:
    def __init__(self, selected: bool = False) -> None:
        self._selected = selected

    def is_selected(self) -> bool:
        return self._selected


class _WebDriverStub:
    """find_elements keyed by (strategy, value) — the only API _find_unique touches."""

    def __init__(self, matches: dict[tuple[str, str], list[_Element]]) -> None:
        self._matches = matches

    def find_elements(self, how: str, what: str) -> list[_Element]:
        return self._matches.get((how, what), [])


class TestSeleniumUniqueResolution:
    def test_single_match_resolves(self) -> None:
        target = Target(role=None, name=None, css="#go")
        el = _Element()
        stub = _WebDriverStub({("css selector", "#go"): [el]})
        assert _find_unique(stub, target) is el

    def test_ambiguous_target_raises_instead_of_first_match(self) -> None:
        # Two buttons named "Go": clicking "the first one" is exactly the
        # hidden-wrong-element failure strict locators exist to surface.
        target = Target(role=None, name=None, css=".go")
        stub = _WebDriverStub({("css selector", ".go"): [_Element(), _Element()]})
        with pytest.raises(DriverError, match="ambiguous"):
            _find_unique(stub, target)

    def test_not_found_still_raises_with_the_tried_locators(self) -> None:
        target = Target(role=None, name=None, css="#nope")
        with pytest.raises(DriverError, match="not found"):
            _find_unique(_WebDriverStub({}), target)

    def test_fallback_strategy_is_only_consulted_when_primary_matches_nothing(self) -> None:
        # role+name primary misses -> css fallback resolves; ambiguity is judged
        # WITHIN the strategy that matched, not across strategies.
        target = Target(role="button", name="Go", css="#go")
        el = _Element()
        from cartogate.nav.selenium_driver import _locator_candidates

        primary, fallback = _locator_candidates(target)
        stub = _WebDriverStub({fallback: [el]})
        assert _find_unique(stub, target) is el
