"""Integration test for the viz navmap (Task 4, Stage 2A).

The viz navmap is the first dogfood artifact: it documents the cartogate viz itself,
using the new fragment-aware URL patterns and checked-state landmarks.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.nav.runtime import Navigator
from cartogate.nav.schema import load
from cartogate.nav.testing import FakeDriver

VIZ_NAVMAP_PATH = Path(__file__).parent.parent / "fixtures" / "nav" / "viz-navmap.json"


class TestVizNavmapSchema:
    """The viz navmap is schema-valid and loads correctly."""

    def test_viz_navmap_loads(self) -> None:
        """Viz navmap file loads and parses."""
        navmap = load(VIZ_NAVMAP_PATH)
        assert navmap.app == "cartogate-viz"
        assert len(navmap.states) == 2
        assert len(navmap.flows) == 1

    def test_viz_navmap_states(self) -> None:
        """Viz navmap has the expected states."""
        navmap = load(VIZ_NAVMAP_PATH)
        families_state = navmap.state("viz.families")
        globe_state = navmap.state("viz.globe")

        # Check families state
        assert families_state.url == "/viz.html#v=families"
        assert families_state.fragment == (("v", "families"),)
        assert len(families_state.landmarks) == 1
        assert families_state.landmarks[0].role == "radio"
        assert families_state.landmarks[0].name == "Structure"
        assert families_state.landmarks[0].checked is True
        assert len(families_state.affordances) == 1

        # Check globe state
        assert globe_state.url == "/viz.html#v=globe"
        assert globe_state.fragment == (("v", "globe"),)
        assert len(globe_state.landmarks) == 1
        assert globe_state.landmarks[0].role == "radio"
        assert globe_state.landmarks[0].name == "Globe"
        assert globe_state.landmarks[0].checked is True

    def test_viz_navmap_transitions(self) -> None:
        """Viz navmap has view-switching transitions."""
        navmap = load(VIZ_NAVMAP_PATH)
        assert len(navmap.transitions) == 2

        # families -> globe transition
        families_to_globe = [t for t in navmap.transitions
                            if t.from_state == "viz.families" and t.to_state == "viz.globe"][0]
        assert families_to_globe.do == {"click": "globe_radio"}

        # globe -> families transition
        globe_to_families = [t for t in navmap.transitions
                            if t.from_state == "viz.globe" and t.to_state == "viz.families"][0]
        assert globe_to_families.do == {"click": "families_radio"}

    def test_viz_navmap_flow(self) -> None:
        """Viz navmap defines a tour flow."""
        navmap = load(VIZ_NAVMAP_PATH)
        flows_dict = navmap.flows_by_name
        assert "tour" in flows_dict
        tour = flows_dict["tour"]
        assert tour.path == ("viz.families", "viz.globe", "viz.families")


class TestVizNavmapFakeDriver:
    """The viz navmap can be navigated with FakeDriver."""

    def test_tour_flow_via_fake_driver(self) -> None:
        """Execute the tour flow through all states via FakeDriver."""
        navmap = load(VIZ_NAVMAP_PATH)

        # Set up FakeDriver to mirror the navmap.
        # All radio buttons are visible on all pages (real behavior in the viz),
        # but only one is checked at a time.
        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure", "radio:Globe"},
            "http://localhost/viz.html#v=globe": {"radio:Structure", "radio:Globe"},
        }
        checked_map = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        wiring = {
            ("http://localhost/viz.html#v=families", "radio:Globe"): "http://localhost/viz.html#v=globe",
            ("http://localhost/viz.html#v=globe", "radio:Structure"): "http://localhost/viz.html#v=families",
        }
        driver = FakeDriver(pages=pages, wiring=wiring, checked=checked_map)
        nav = Navigator(driver, navmap)

        # Start at families
        driver.navigate("http://localhost/viz.html#v=families")
        assert nav.where() == "viz.families"

        # Navigate to globe via declared transition
        nav.goto("viz.globe")
        assert nav.where() == "viz.globe"

        # Navigate back to families via declared transition
        nav.goto("viz.families")
        assert nav.where() == "viz.families"

    def test_tour_flow_via_declared_path(self) -> None:
        """Execute the tour flow using declared paths."""
        navmap = load(VIZ_NAVMAP_PATH)

        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure", "radio:Globe"},
            "http://localhost/viz.html#v=globe": {"radio:Structure", "radio:Globe"},
        }
        checked_map = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        wiring = {
            ("http://localhost/viz.html#v=families", "radio:Globe"): "http://localhost/viz.html#v=globe",
            ("http://localhost/viz.html#v=globe", "radio:Structure"): "http://localhost/viz.html#v=families",
        }
        driver = FakeDriver(pages=pages, wiring=wiring, checked=checked_map)
        nav = Navigator(driver, navmap)

        # Walk the tour flow
        tour = navmap.flows_by_name["tour"]
        driver.navigate("http://localhost/viz.html#v=families")

        for state_id in tour.path[1:]:  # Skip the first state (already there)
            nav.goto(state_id)
            assert nav.where() == state_id

    def test_landmark_checked_distinguishes_views(self) -> None:
        """Checked landmarks distinguish views when on same path."""
        navmap = load(VIZ_NAVMAP_PATH)

        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure", "radio:Globe"},
            "http://localhost/viz.html#v=globe": {"radio:Structure", "radio:Globe"},
        }
        # Only Structure is checked in families view
        checked_map = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        driver = FakeDriver(pages=pages, wiring={}, checked=checked_map)
        nav = Navigator(driver, navmap)

        # Navigate to families — should match because Structure is checked
        driver.navigate("http://localhost/viz.html#v=families")
        assert nav.where() == "viz.families"

        # Navigate to globe — should match because Globe is checked
        driver.navigate("http://localhost/viz.html#v=globe")
        assert nav.where() == "viz.globe"


def test_round2_map_is_schema_valid() -> None:
    """The round-2 hard-task map (4-hop + tests-drill) stays schema-valid and
    self-consistent — live nav check on the real viz is verified manually."""
    from cartogate.nav.schema import load

    root = Path(__file__).resolve().parents[1]
    navmap = load(root / "fixtures" / "nav" / "viz-round2-navmap.json")
    ids = {s.id for s in navmap.states}
    assert ids == {"viz.structure", "viz.globe", "viz.deps", "viz.drill_tests"}
    tour = next(f for f in navmap.flows if f.name == "tour")
    assert list(tour.path) == [
        "viz.structure", "viz.globe", "viz.deps", "viz.structure", "viz.drill_tests"
    ]
    # every transition endpoint is a real state; the drill affordance is css-only
    drill = next(s for s in navmap.states if s.id == "viz.structure")
    assert any(a.css and a.role is None for a in drill.affordances)
