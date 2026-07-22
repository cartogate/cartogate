"""cartogate.nav — deterministic UI navigation maps (spec 2026-07-18).

Optional ``[nav]`` extra. This module's ``__all__`` is the curated public API:
the names re-exported here are the supported import surface. Everything else
(``discover``, ``crawler``, private helpers) is internal and may change.

Typical use::

    from cartogate.nav import Navigator, load, PlaywrightDriver

    navmap = load(Path("navmap.json"))
    nav = Navigator(navmap, PlaywrightDriver("http://localhost:3000"))
    nav.goto("checkout")

The adapters are re-exported for convenience but stay lazy: importing this
package never imports playwright or selenium — only *instantiating* an adapter
requires its extra (``cartogate[nav]`` / ``cartogate[nav-selenium]``).
"""

from __future__ import annotations

from cartogate.nav.driver import Driver, DriverError, Target, Wait
from cartogate.nav.playwright_driver import PlaywrightDriver
from cartogate.nav.runtime import LOST, NavigationError, Navigator
from cartogate.nav.schema import (
    Affordance,
    Flow,
    Landmark,
    NavMap,
    NavMapError,
    State,
    Transition,
    load,
    parse_navmap,
)
from cartogate.nav.selenium_driver import SeleniumDriver
from cartogate.nav.testing import FakeDriver

__all__ = [
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
    # Adapters (lazy — the extra is needed only to instantiate)
    "PlaywrightDriver",
    "SeleniumDriver",
    "FakeDriver",
]
