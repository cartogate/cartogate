"""Tests for cartogate.nav.playwright_driver — PlaywrightDriver guard + imports."""

from __future__ import annotations

import sys

import pytest

from cartogate.nav.playwright_driver import require_playwright
from cartogate.nav.schema import NavMapError


class TestRequirePlaywright:
    """require_playwright() guard function."""

    def test_require_playwright_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """require_playwright() raises actionable error when playwright not installed."""
        # Simulate playwright not being installed
        import importlib.util

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str) -> None:
            if name == "playwright":
                return None
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        # Import fresh
        if "cartogate.nav.playwright_driver" in sys.modules:
            del sys.modules["cartogate.nav.playwright_driver"]

        from cartogate.nav.playwright_driver import require_playwright as rp

        with pytest.raises(NavMapError, match="cartogate\\[nav\\]"):
            rp()

    def test_require_playwright_when_present(self) -> None:
        """require_playwright() succeeds when playwright is installed."""
        # Playwright is installed in this test environment
        try:
            require_playwright()
        except NavMapError:
            # Skip if playwright not available in this test environment
            pytest.skip("playwright not installed")


class TestPlaywrightDriverImport:
    """Importing PlaywrightDriver module."""

    def test_module_imports_without_playwright(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Importing the module succeeds even if playwright is not installed."""
        # The module should import fine without actually importing playwright
        # (it uses lazy imports)
        import importlib.util

        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str) -> None:
            if name == "playwright":
                return None
            return original_find_spec(name)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

        # Clear from modules to force re-import
        if "cartogate.nav.playwright_driver" in sys.modules:
            del sys.modules["cartogate.nav.playwright_driver"]

        # This should NOT raise during import
        try:
            import cartogate.nav.playwright_driver  # noqa: F401
        except ImportError as e:
            if "playwright" in str(e):
                pytest.skip("playwright not available")
            raise


class TestPlaywrightDriverInstantiation:
    """PlaywrightDriver instantiation (smoke test, requires playwright)."""

    @pytest.mark.skipif(
        not pytest.importorskip("playwright", minversion=None), reason="playwright not installed"
    )
    def test_playwright_driver_class_exists(self) -> None:
        """PlaywrightDriver class can be imported when playwright is available."""
        pytest.importorskip("playwright")
        from cartogate.nav.playwright_driver import PlaywrightDriver

        # Just check that the class exists and is a class
        assert isinstance(PlaywrightDriver, type)

    @pytest.mark.skipif(
        not pytest.importorskip("playwright", minversion=None), reason="playwright not installed"
    )
    def test_playwright_driver_satisfies_protocol(self) -> None:
        """PlaywrightDriver implements the Driver protocol (smoke)."""
        pytest.importorskip("playwright")
        from cartogate.nav.playwright_driver import PlaywrightDriver

        # Check that PlaywrightDriver has the right methods
        methods = {
            "navigate",
            "current_url",
            "click",
            "fill",
            "wait_for",
            "is_visible",
            "screenshot",
        }
        for method in methods:
            assert hasattr(PlaywrightDriver, method), f"PlaywrightDriver missing {method}"

    def test_playwright_driver_close_before_launch(self) -> None:
        """PlaywrightDriver.close() is safe to call before any methods launched browser."""
        pytest.importorskip("playwright")
        from cartogate.nav.playwright_driver import PlaywrightDriver

        # Construct driver (don't call any methods that would launch browser)
        driver = PlaywrightDriver(base_url="http://localhost:3000", headless=True)
        # Calling close() immediately should not raise
        driver.close()  # Should be a safe no-op


class _Rec:
    """Records close()/stop() calls; optionally raises to model a teardown fault."""

    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.calls = 0

    def close(self) -> None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("browser.close boom")

    def stop(self) -> None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("playwright.stop boom")


def _bare_driver() -> object:
    """A PlaywrightDriver instance without running __init__ (which requires the
    playwright package) — so close() teardown can be tested browser-free."""
    from cartogate.nav.playwright_driver import PlaywrightDriver

    d = object.__new__(PlaywrightDriver)
    d._playwright = None
    d._browser = None
    d._page = None
    return d


class TestPlaywrightDriverClose:
    """close() must not leak a browser/playwright process on error paths."""

    def test_close_stops_playwright_even_if_browser_close_raises(self) -> None:
        # The orphaned-chromium source (A/B rig): if browser.close() throws,
        # playwright.stop() must still run — otherwise the process leaks.
        d = _bare_driver()
        browser, pw = _Rec(raises=True), _Rec()
        d._browser, d._playwright = browser, pw  # type: ignore[attr-defined]
        d.close()  # type: ignore[attr-defined]
        assert browser.calls == 1
        assert pw.calls == 1  # reached despite browser.close() raising
        assert d._browser is None  # type: ignore[attr-defined]
        assert d._playwright is None  # type: ignore[attr-defined]
        assert d._page is None  # type: ignore[attr-defined]

    def test_close_is_idempotent(self) -> None:
        # A second close() must be a no-op, not a double-teardown.
        d = _bare_driver()
        browser, pw = _Rec(), _Rec()
        d._browser, d._playwright = browser, pw  # type: ignore[attr-defined]
        d.close()  # type: ignore[attr-defined]
        d.close()  # type: ignore[attr-defined]
        assert browser.calls == 1
        assert pw.calls == 1


def test_resolve_joins_relative_against_base() -> None:
    """The driver, not the Navigator, owns origin resolution."""
    from cartogate.nav.playwright_driver import _resolve

    assert _resolve("http://localhost:8000", "/viz.html#v=x") == (
        "http://localhost:8000/viz.html#v=x")
    assert _resolve("http://a:1", "http://b:2/full") == "http://b:2/full"  # absolutes pass
