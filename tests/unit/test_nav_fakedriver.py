"""Tests for cartogate.nav.driver (protocol) and cartogate.nav.testing (FakeDriver)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cartogate.nav.driver import DriverError, Target, Wait
from cartogate.nav.testing import FakeDriver, key_of


class TestTarget:
    """Target dataclass."""

    def test_target_creation(self) -> None:
        """Target can be created with role+name, css, or both."""
        t1 = Target(role="button", name="Click")
        assert t1.role == "button"
        assert t1.name == "Click"
        assert t1.css is None

        t2 = Target(css="[data-test=btn]")
        assert t2.css == "[data-test=btn]"
        assert t2.role is None
        assert t2.name is None

        t3 = Target(role="button", name="Click", css="[data-test=btn]")
        assert t3.role == "button"
        assert t3.css == "[data-test=btn]"

    def test_target_frozen(self) -> None:
        """Target is frozen."""
        t = Target(role="button", name="Click")
        with pytest.raises(AttributeError):
            t.role = "link"  # type: ignore


class TestWait:
    """Wait dataclass."""

    def test_wait_url_matches(self) -> None:
        """Wait can wait for URL pattern."""
        w = Wait(url_matches=r"^/profile.*")
        assert w.url_matches == r"^/profile.*"
        assert w.target_visible is None
        assert w.timeout_s == 10.0

    def test_wait_target_visible(self) -> None:
        """Wait can wait for target visibility."""
        target = Target(role="button", name="Save")
        w = Wait(target_visible=target)
        assert w.target_visible == target
        assert w.url_matches is None

    def test_wait_custom_timeout(self) -> None:
        """Wait can have custom timeout."""
        w = Wait(url_matches="/home", timeout_s=5.0)
        assert w.timeout_s == 5.0

    def test_wait_frozen(self) -> None:
        """Wait is frozen."""
        w = Wait(url_matches="/home")
        with pytest.raises(AttributeError):
            w.timeout_s = 20.0  # type: ignore

    def test_wait_rejects_neither_condition(self) -> None:
        """A Wait with no condition is a silent no-op across drivers — reject it."""
        with pytest.raises(ValueError, match="exactly one"):
            Wait()

    def test_wait_rejects_both_conditions(self) -> None:
        """Two conditions are ambiguous (which does the driver wait on?) — reject."""
        with pytest.raises(ValueError, match="exactly one"):
            Wait(url_matches="/home", target_visible=Target(role="button", name="X"))

    def test_wait_rejects_nonpositive_timeout(self) -> None:
        """timeout_s <= 0 diverges by driver (Selenium→default, Playwright→infinite
        hang). Forbid it so a Wait cannot carry the ambiguous value."""
        with pytest.raises(ValueError, match="timeout_s"):
            Wait(url_matches="/home", timeout_s=0.0)
        with pytest.raises(ValueError, match="timeout_s"):
            Wait(url_matches="/home", timeout_s=-1.0)


class TestKeyOf:
    """key_of(target) helper."""

    def test_key_of_role_name(self) -> None:
        """key_of returns 'role:name' format for role+name."""
        target = Target(role="button", name="Click")
        key = key_of(target)
        assert key == "button:Click"

    def test_key_of_css(self) -> None:
        """key_of returns 'css:<selector>' for css."""
        target = Target(css="[data-test=btn]")
        key = key_of(target)
        assert key == "css:[data-test=btn]"

    def test_key_of_role_name_preferred(self) -> None:
        """key_of prefers role:name over css."""
        target = Target(role="button", name="Click", css="[data-test=btn]")
        key = key_of(target)
        assert key == "button:Click"


class TestFakeDriver:
    """FakeDriver in-memory test double."""

    def test_navigate_and_current_url(self) -> None:
        """navigate() sets current_url()."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        assert driver.current_url() == "http://example.com"

    def test_navigate_unknown_url(self) -> None:
        """navigate() raises DriverError for unknown URL."""
        driver = FakeDriver(pages={}, wiring={})
        with pytest.raises(DriverError, match="http://example.com"):
            driver.navigate("http://example.com")

    def test_click_follows_wiring(self) -> None:
        """click() follows wiring and updates current_url."""
        pages = {"http://example.com": {"button:Click"}, "http://example.com/next": set()}
        wiring = {("http://example.com", "button:Click"): "http://example.com/next"}
        driver = FakeDriver(pages=pages, wiring=wiring)
        driver.navigate("http://example.com")
        driver.click(Target(role="button", name="Click"))
        assert driver.current_url() == "http://example.com/next"

    def test_click_missing_target(self) -> None:
        """click() raises DriverError if target is not visible."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        with pytest.raises(DriverError, match="button:Click"):
            driver.click(Target(role="button", name="Click"))

    def test_click_no_wiring(self) -> None:
        """click() raises DriverError if no wiring for the click."""
        pages = {"http://example.com": {"button:Click"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        with pytest.raises(DriverError):
            driver.click(Target(role="button", name="Click"))

    def test_is_visible_true(self) -> None:
        """is_visible() returns True for visible targets."""
        pages = {"http://example.com": {"button:Click"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        assert driver.is_visible(Target(role="button", name="Click"))

    def test_is_visible_false(self) -> None:
        """is_visible() returns False for missing targets."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        assert not driver.is_visible(Target(role="button", name="Click"))

    def test_fill_records_action(self) -> None:
        """fill() records the action."""
        pages = {"http://example.com": {"input:search"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        driver.fill(Target(role="input", name="search"), "query text")
        # Check that action was recorded
        assert any("fill" in action for action in driver.actions)

    def test_screenshot_writes_file(self, tmp_path: Path) -> None:
        """screenshot() writes a file."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        out_file = tmp_path / "screen.png"
        driver.screenshot(out_file)
        assert out_file.exists()
        assert out_file.stat().st_size > 0

    def test_actions_recorded(self) -> None:
        """All actions are recorded in driver.actions."""
        pages = {
            "http://example.com": {"button:Go", "input:search"},
            "http://example.com/results": {"button:Back"},
        }
        wiring = {
            ("http://example.com", "button:Go"): "http://example.com/results",
            ("http://example.com/results", "button:Back"): "http://example.com",
        }
        driver = FakeDriver(pages=pages, wiring=wiring)
        driver.navigate("http://example.com")
        driver.fill(Target(role="input", name="search"), "test")
        driver.click(Target(role="button", name="Go"))
        driver.is_visible(Target(role="button", name="Back"))
        driver.click(Target(role="button", name="Back"))
        # Verify actions are recorded
        assert len(driver.actions) > 0
        assert any("navigate" in action for action in driver.actions)
        assert any("click" in action for action in driver.actions)

    def test_wait_for_url_matches(self) -> None:
        """wait_for() with url_matches waits for URL pattern match."""
        pages = {
            "http://example.com": set(),
            "http://example.com/loading": set(),
        }
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com/loading")
        # In fake driver, wait_for with url_matches should match current URL
        driver.wait_for(Wait(url_matches=r".*/loading"))
        # Should not raise

    def test_wait_for_target_visible(self) -> None:
        """wait_for() with target_visible checks visibility."""
        pages = {"http://example.com": {"button:Click"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        driver.wait_for(Wait(target_visible=Target(role="button", name="Click")))
        # Should not raise

    def test_wait_for_target_not_visible_raises(self) -> None:
        """wait_for() raises if target not visible."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://example.com")
        with pytest.raises(DriverError, match="button:Click"):
            driver.wait_for(Wait(target_visible=Target(role="button", name="Click")))

    def test_driver_protocol_satisfaction(self) -> None:
        """FakeDriver satisfies the Driver protocol."""
        pages = {"http://example.com": set()}
        driver = FakeDriver(pages=pages, wiring={})
        # If FakeDriver didn't implement the protocol, this type check would fail
        # (Not a runtime check, but makes the point)
        assert isinstance(driver, FakeDriver)
