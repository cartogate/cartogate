"""The curated ``cartogate.nav`` public API surface.

This is the compatibility contract: the names re-exported from the package are
what users may import, and — once released — what we owe stability to. Pinning
the set here makes any addition/removal a deliberate, reviewed act rather than
an accident of where a symbol happened to live.
"""

from __future__ import annotations

_EXPECTED_SURFACE = {
    # Core navigation
    "Navigator",
    "LOST",
    "NavigationError",
    # Map model + loading
    "load",
    "parse_navmap",
    "NavMap",
    "State",
    "Landmark",
    "Affordance",
    "Transition",
    "Flow",
    "NavMapError",
    # Driver seam
    "Driver",
    "Target",
    "Wait",
    "DriverError",
    # Adapters
    "PlaywrightDriver",
    "SeleniumDriver",
    "FakeDriver",
}


def test_all_matches_the_pinned_surface() -> None:
    import cartogate.nav as nav

    assert set(nav.__all__) == _EXPECTED_SURFACE


def test_every_exported_name_is_importable() -> None:
    import cartogate.nav as nav

    for name in nav.__all__:
        assert hasattr(nav, name), f"{name!r} is in __all__ but not importable"


def test_importing_the_package_needs_no_optional_extras() -> None:
    # Re-exporting the adapters must not drag playwright/selenium into a plain
    # ``import cartogate.nav`` — the extras stay lazy. The adapter modules import
    # cleanly without their packages; only instantiation requires them.
    import cartogate.nav as nav

    # The class objects resolve (definition-time, no extra needed) ...
    assert isinstance(nav.PlaywrightDriver, type)
    assert isinstance(nav.SeleniumDriver, type)
    # ... and the canonical entry points are the real objects.
    from cartogate.nav.runtime import Navigator
    from cartogate.nav.schema import load

    assert nav.Navigator is Navigator
    assert nav.load is load
