"""Tests for cartogate.nav.runtime — deterministic where/goto/affordances/capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate.nav.driver import Target
from cartogate.nav.runtime import LOST, NavigationError, Navigator
from cartogate.nav.schema import (
    Affordance,
    Flow,
    Landmark,
    NavMap,
    State,
    Transition,
)
from cartogate.nav.testing import FakeDriver


# Fixture: a 4-state map (home, list, detail(:id), settings) for testing
def fixture_navmap() -> NavMap:
    """A realistic 4-state navigation map with flows."""
    states = (
        State(
            id="home",
            url="/",
            landmarks=(Landmark(role="heading", name="Welcome"),),
            affordances=(
                Affordance(ref="a1", role="button", name="View List", provenance="extracted"),
            ),
            provenance="extracted",
        ),
        State(
            id="list",
            url="/items",
            landmarks=(Landmark(role="heading", name="Items"),),
            affordances=(
                Affordance(
                    ref="a2",
                    role="link",
                    name="Item 1",
                    css="[data-id='1']",
                    provenance="extracted",
                ),
                Affordance(
                    ref="a3", role="link", name="Home", provenance="extracted"
                ),
            ),
            provenance="extracted",
        ),
        State(
            id="detail",
            url="/items/:id",
            landmarks=(Landmark(role="heading", name="Item Details"),),
            affordances=(
                Affordance(ref="a4", role="button", name="Back to List", provenance="extracted"),
                Affordance(ref="a5", role="link", name="Settings", provenance="extracted"),
            ),
            provenance="extracted",
        ),
        State(
            id="settings",
            url="/settings",
            landmarks=(Landmark(role="heading", name="Settings"),),
            affordances=(
                Affordance(ref="a6", role="link", name="Home", provenance="extracted"),
            ),
            provenance="extracted",
        ),
    )

    transitions = (
        Transition(from_state="home", do={"click": "a1"}, to_state="list"),
        Transition(from_state="list", do={"click": "a2"}, to_state="detail"),
        Transition(from_state="detail", do={"click": "a4"}, to_state="list"),
        Transition(from_state="list", do={"click": "a3"}, to_state="home"),
        Transition(from_state="detail", do={"click": "a5"}, to_state="settings"),
        Transition(from_state="settings", do={"click": "a6"}, to_state="home"),
    )

    flows = (
        Flow(name="happy", path=("home", "list", "detail", "settings")),
        Flow(name="back", path=("detail", "list", "home")),
    )

    return NavMap(
        app="testapp",
        states=states,
        transitions=transitions,
        flows=flows,
        raw={"version": 1, "app": "testapp"},
    )


def fixture_fake_driver() -> FakeDriver:
    """FakeDriver set up for fixture_navmap."""
    pages = {
        "http://localhost/": {"heading:Welcome", "button:View List"},
        "http://localhost/items": {"heading:Items", "link:Item 1", "link:Home"},
        "http://localhost/items/1": {
            "heading:Item Details",
            "button:Back to List",
            "link:Settings",
        },
        "http://localhost/settings": {"heading:Settings", "link:Home"},
    }
    wiring = {
        ("http://localhost/", "button:View List"): "http://localhost/items",
        ("http://localhost/items", "link:Item 1"): "http://localhost/items/1",
        ("http://localhost/items/1", "button:Back to List"): "http://localhost/items",
        ("http://localhost/items", "link:Home"): "http://localhost/",
        ("http://localhost/items/1", "link:Settings"): "http://localhost/settings",
        ("http://localhost/settings", "link:Home"): "http://localhost/",
    }
    return FakeDriver(pages=pages, wiring=wiring)


class TestURLMatching:
    """URL pattern matching with literal colons vs. param placeholders."""

    def test_literal_colon_exact_match_only(self) -> None:
        """URL with literal colon (/user:admin) matches ONLY that exact URL."""
        # Create a state with a literal colon (no :param syntax)
        states = (
            State(
                id="admin",
                url="/user:admin",
                landmarks=(Landmark(role="heading", name="Admin"),),
                affordances=(),
                provenance="extracted",
            ),
        )
        navmap = NavMap(
            app="testapp",
            states=states,
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp"},
        )

        # Should match /user:admin exactly
        pages = {"http://localhost/user:admin": {"heading:Admin"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/user:admin")
        nav = Navigator(driver, navmap, settle_s=0.0)
        assert nav.where() == "admin"

        # Should NOT match /userX or /users or other variants
        pages2 = {"http://localhost/userX": {"heading:Admin"}}
        driver2 = FakeDriver(pages=pages2, wiring={})
        driver2.navigate("http://localhost/userX")
        nav2 = Navigator(driver2, navmap)
        assert nav2.where() == "lost"

    def test_param_placeholder_matches_path_segment(self) -> None:
        """URL pattern /items/:id matches /items/7 but not /items/7/extra."""
        states = (
            State(
                id="detail",
                url="/items/:id",
                landmarks=(Landmark(role="heading", name="Detail"),),
                affordances=(),
                provenance="extracted",
            ),
        )
        navmap = NavMap(
            app="testapp",
            states=states,
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp"},
        )

        # Should match /items/7
        pages = {"http://localhost/items/7": {"heading:Detail"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/items/7")
        nav = Navigator(driver, navmap, settle_s=0.0)
        assert nav.where() == "detail"

        # Should NOT match /items/7/extra (extra segment)
        pages2 = {"http://localhost/items/7/extra": {"heading:Detail"}}
        driver2 = FakeDriver(pages=pages2, wiring={})
        driver2.navigate("http://localhost/items/7/extra")
        nav2 = Navigator(driver2, navmap)
        assert nav2.where() == "lost"


class TestWhereExact:
    """Navigator.where() — exact URL match."""

    def test_where_exact_match(self) -> None:
        """where() matches exact URL."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        driver.navigate("http://localhost/")
        nav = Navigator(driver, navmap, settle_s=0.0)
        state_id = nav.where()
        assert state_id == "home"

    def test_where_param_match(self) -> None:
        """where() matches URL patterns with params (:id)."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        driver.navigate("http://localhost/items/1")
        nav = Navigator(driver, navmap, settle_s=0.0)
        state_id = nav.where()
        # Should match /items/:id pattern
        assert state_id == "detail"

    def test_where_landmark_missing_is_lost(self) -> None:
        """where() returns LOST if landmark is missing."""
        navmap = fixture_navmap()
        # Create a driver where the landmark is missing
        pages = {
            "http://localhost/": set(),  # No heading:Welcome
        }
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/")
        nav = Navigator(driver, navmap, settle_s=0.0)
        state_id = nav.where()
        assert state_id == "lost"

    def test_where_no_match_is_lost(self) -> None:
        """where() returns LOST if URL doesn't match any state."""
        navmap = fixture_navmap()
        # Create a driver with an unknown URL
        pages = {"http://localhost/unknown": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/unknown")
        nav = Navigator(driver, navmap, settle_s=0.0)
        state_id = nav.where()
        assert state_id == "lost"

    def test_where_specificity_tie_break(self) -> None:
        """where() returns most-specific match (fewest params), then lexicographic."""
        # Create a map where multiple patterns could match
        states = (
            State(
                id="list",
                url="/items",
                landmarks=(Landmark(role="heading", name="List"),),
                affordances=(),
                provenance="extracted",
            ),
            State(
                id="detail",
                url="/items/:id",
                landmarks=(Landmark(role="heading", name="Detail"),),
                affordances=(),
                provenance="extracted",
            ),
        )

        navmap = NavMap(
            app="testapp",
            states=states,
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp"},
        )

        # Test exact match (0 params) is more specific than param match (1 param)
        pages = {"http://localhost/items": {"heading:List", "heading:Detail"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/items")
        nav = Navigator(driver, navmap, settle_s=0.0)
        # /items should match the exact pattern, not /items/:id
        state_id = nav.where()
        assert state_id == "list"


class TestGotoDirect:
    """Navigator.goto() — direct URL navigation."""

    def test_goto_direct_url_happy(self) -> None:
        """goto() navigates directly to a state's URL and verifies landmarks."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        nav = Navigator(driver, navmap, settle_s=0.0)
        result = nav.goto("list")
        assert result == "list"
        assert driver.current_url() == "http://localhost/items"

    def test_goto_direct_url_missing_landmark(self) -> None:
        """goto() raises NavigationError if landmark is missing after navigation."""
        navmap = fixture_navmap()
        # Driver doesn't have the landmark on /items
        pages = {"http://localhost/items": set()}
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)
        with pytest.raises(NavigationError, match="Items"):
            nav.goto("list")


class TestGotoBFS:
    """Navigator.goto() — multi-hop BFS."""

    def test_goto_multihop_happy(self) -> None:
        """goto() uses BFS to reach a state via known transitions."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        driver.navigate("http://localhost/")
        nav = Navigator(driver, navmap, settle_s=0.0)
        result = nav.goto("detail")
        # BFS should find: home -> list (click a1) -> detail (click a2)
        assert result == "detail"
        # Verify only declared edges were executed
        # Should have navigated to /, then clicked to /items, then clicked to /items/1
        actions = driver.actions
        assert any("navigate" in action for action in actions)
        # a1 is button:View List, a2 is link:Item 1
        assert any("click" in action and "View List" in action for action in actions)
        assert any("click" in action and "Item 1" in action for action in actions)

    def test_goto_bfs_only_declared_edges(self) -> None:
        """goto() never explores — only follows declared transitions."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        driver.navigate("http://localhost/")
        nav = Navigator(driver, navmap, settle_s=0.0)
        nav.goto("detail")
        # Verify actions list only contains declared edge executions
        # No random clicking or URL guessing
        actions = driver.actions
        for action in actions:
            if "click" in action:
                # Should only see declared transitions: View List or Item 1
                assert "View List" in action or "Item 1" in action
                # These are the declared edges from home/list to their targets

    def test_goto_unreachable_state(self) -> None:
        """goto() raises NavigationError if state is unreachable (param-based URLs)."""
        # Create a map with unreachable states (use param-based URLs)
        states = (
            State(
                id="a",
                url="/a/:id",
                landmarks=(Landmark(role="heading", name="A"),),
                affordances=(),
                provenance="extracted",
            ),
            State(
                id="b",
                url="/b/:id",
                landmarks=(Landmark(role="heading", name="B"),),
                affordances=(),
                provenance="extracted",
            ),
        )
        navmap = NavMap(
            app="testapp",
            states=states,
            transitions=(),  # no transitions!
            flows=(),
            raw={"version": 1, "app": "testapp"},
        )

        pages = {"http://localhost/a/1": {"heading:A"}, "http://localhost/b/1": {"heading:B"}}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/a/1")
        nav = Navigator(driver, navmap, settle_s=0.0)

        with pytest.raises(NavigationError):
            nav.goto("b")

    def test_goto_max_hops_exceeded(self) -> None:
        """goto() raises NavigationError if max_hops exceeded."""
        # Create a circular chain longer than max_hops (use param-based URLs)
        states = (
            State(
                id="s1",
                url="/s/:id",
                landmarks=(Landmark(role="heading", name="S1"),),
                affordances=(Affordance(ref="a1", role="button", name="Next"),),
                provenance="extracted",
            ),
            State(
                id="s2",
                url="/s/:id",
                landmarks=(Landmark(role="heading", name="S2"),),
                affordances=(Affordance(ref="a2", role="button", name="Next"),),
                provenance="extracted",
            ),
            State(
                id="s3",
                url="/s/:id",
                landmarks=(Landmark(role="heading", name="S3"),),
                affordances=(Affordance(ref="a3", role="button", name="Next"),),
                provenance="extracted",
            ),
        )

        # Circular: s1 -> s2 -> s3 -> s1 ...
        transitions = (
            Transition(from_state="s1", do={"click": "a1"}, to_state="s2"),
            Transition(from_state="s2", do={"click": "a2"}, to_state="s3"),
            Transition(from_state="s3", do={"click": "a3"}, to_state="s1"),
        )

        navmap = NavMap(
            app="testapp",
            states=states,
            transitions=transitions,
            flows=(),
            raw={"version": 1, "app": "testapp"},
        )

        pages = {
            "http://localhost/s/1": {"heading:S1", "button:Next"},
            "http://localhost/s/2": {"heading:S2", "button:Next"},
            "http://localhost/s/3": {"heading:S3", "button:Next"},
        }
        wiring = {
            ("http://localhost/s/1", "button:Next"): "http://localhost/s/2",
            ("http://localhost/s/2", "button:Next"): "http://localhost/s/3",
            ("http://localhost/s/3", "button:Next"): "http://localhost/s/1",
        }
        driver = FakeDriver(pages=pages, wiring=wiring)
        driver.navigate("http://localhost/s/1")
        nav = Navigator(driver, navmap, settle_s=0.0, max_hops=3)

        # Trying to reach s3 from s1 requires: s1 -> s2 (1) -> s3 (2) = 2 hops, OK
        result = nav.goto("s3")
        assert result == "s3"

        # Now with max_hops=1, trying to reach s3 should fail (need 2 hops)
        driver.navigate("http://localhost/s/1")
        nav = Navigator(driver, navmap, settle_s=0.0, max_hops=1)
        with pytest.raises(NavigationError, match="hops"):
            nav.goto("s3")

    def test_goto_broken_landmark_at_hop(self) -> None:
        """goto() raises NavigationError naming state+landmark if verification fails mid-path."""
        navmap = fixture_navmap()
        # Driver with missing landmark at detail state
        pages = {
            "http://localhost/": {"heading:Welcome"},
            "http://localhost/items": {"heading:Items"},
            "http://localhost/items/1": set(),  # Missing "heading:Item Details"
        }
        wiring = {
            ("http://localhost/", "button:View List"): "http://localhost/items",
            ("http://localhost/items", "link:Item 1"): "http://localhost/items/1",
        }
        driver = FakeDriver(pages=pages, wiring=wiring)
        driver.navigate("http://localhost/")
        nav = Navigator(driver, navmap, settle_s=0.0)

        with pytest.raises(NavigationError, match="detail"):
            nav.goto("detail")


class TestAffordances:
    """Navigator.affordances() — list affordances with live visibility."""

    def test_affordances_happy(self) -> None:
        """affordances() returns map affordances for current state."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        driver.navigate("http://localhost/items")
        nav = Navigator(driver, navmap, settle_s=0.0)
        affs = nav.affordances()
        assert len(affs) > 0
        # Should return affordances from 'list' state
        ref_set = {aff.ref for aff in affs}
        assert "a2" in ref_set  # "Item 1" link
        assert "a3" in ref_set  # "Home" link

    def test_affordances_live_flagging(self) -> None:
        """affordances() returns all affordances (caller checks visibility separately)."""
        navmap = fixture_navmap()
        # Driver where one affordance is missing
        pages = {"http://localhost/items": {"heading:Items"}}  # No links visible
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/items")
        nav = Navigator(driver, navmap, settle_s=0.0)
        affs = nav.affordances()
        # Should return the affordances from list state
        assert len(affs) == 2  # a2 and a3
        # Caller can check visibility separately
        assert not driver.is_visible(Target(role="link", name="Item 1"))

    def test_affordances_lost_empty(self) -> None:
        """affordances() returns empty list when lost."""
        navmap = fixture_navmap()
        # Create a driver with an unknown URL
        pages = {"http://localhost/unknown": set()}
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/unknown")
        nav = Navigator(driver, navmap, settle_s=0.0)
        affs = nav.affordances()
        assert len(affs) == 0


class FlakyCheckedDriver(FakeDriver):
    """is_checked returns False for the first N probes, then the truth —
    simulates probing a page mid-render (the E2E flake's trigger)."""

    def __init__(self, *args: object, flaky_probes: int = 2, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._flaky_left = flaky_probes

    def is_checked(self, target: Target) -> bool:
        if self._flaky_left > 0:
            self._flaky_left -= 1
            return False
        return super().is_checked(target)


class TestWhereSettling:
    """goto's state detection must survive transient probe failures — a
    single-shot LOST sent it into the direct-nav fallback, which dead-ends on
    same-document fragment navigation (E2E root cause, live-probed 2026-07-20).
    """

    def _checked_map(self) -> NavMap:
        states = (
            State(
                id="a",
                url="/page#v=a",
                landmarks=(Landmark(role="radio", name="A", checked=True),),
                affordances=(
                    Affordance(ref="to_b", role="radio", name="B", provenance="declared"),
                ),
                provenance="declared",
            ),
            State(
                id="b",
                url="/page#v=b",
                landmarks=(Landmark(role="radio", name="B", checked=True),),
                affordances=(),
                provenance="declared",
            ),
        )
        transitions = (Transition(from_state="a", do={"click": "to_b"}, to_state="b"),)
        return NavMap(
            app="t", states=states, transitions=transitions, flows=(),
            raw={"version": 1, "app": "t"},
        )

    def test_transient_probe_failure_still_takes_the_click_path(self) -> None:
        pages = {
            "http://localhost/page#v=a": {"radio:A", "radio:B"},
            "http://localhost/page#v=b": {"radio:A", "radio:B"},
        }
        wiring = {("http://localhost/page#v=a", "radio:B"): "http://localhost/page#v=b"}
        checked = {
            "http://localhost/page#v=a": {"radio:A"},
            "http://localhost/page#v=b": {"radio:B"},
        }
        driver = FlakyCheckedDriver(
            pages=pages, wiring=wiring, checked=checked, flaky_probes=2
        )
        driver.navigate("http://localhost/page#v=a")
        nav = Navigator(driver, self._checked_map(), settle_s=2.0)
        nav.goto("b")
        # The click path was taken — never the direct-nav fallback:
        assert any(a.startswith("click:") for a in driver.actions)
        assert driver.actions.count("navigate: http://localhost/page#v=a") == 1

    def test_structurally_unknown_url_is_lost_immediately(self) -> None:
        # No settle stall when the URL matches NO state pattern — fresh
        # browsers must not pay the full settle window before the fallback.
        import time as _time

        pages = {
            "http://localhost/elsewhere": set(),
            "http://localhost/page#v=a": {"radio:A", "radio:B"},
        }
        driver = FakeDriver(
            pages=pages, wiring={},
            checked={"http://localhost/page#v=a": {"radio:A"}},
        )
        driver.navigate("http://localhost/elsewhere")
        nav = Navigator(driver, self._checked_map(), settle_s=5.0)
        start = _time.monotonic()
        nav.goto("a")  # falls back to direct nav, but FAST
        assert _time.monotonic() - start < 2.0


class TestCapture:
    """Navigator.capture() — screenshot + sealed evidence bundle."""

    def test_capture_happy(self, tmp_path: Path) -> None:
        """capture() returns bundle with state, url, map_hash, image path, hash."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        nav = Navigator(driver, navmap, settle_s=0.0)
        result = nav.capture("home", tmp_path)

        assert result["state"] == "home"
        assert "url" in result
        assert "map_hash" in result
        assert "image_path" in result
        assert "image_blake2b" in result
        assert Path(result["image_path"]).exists()
        assert len(result["image_blake2b"]) == 128  # blake2b hex

    def test_capture_file_written(self, tmp_path: Path) -> None:
        """capture() writes screenshot file to out_dir."""
        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        nav = Navigator(driver, navmap, settle_s=0.0)
        result = nav.capture("home", tmp_path)

        screenshot_path = Path(result["image_path"])
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 0

    def test_capture_hash_matches_file(self, tmp_path: Path) -> None:
        """capture() hash matches actual file bytes."""
        import hashlib

        navmap = fixture_navmap()
        driver = fixture_fake_driver()
        nav = Navigator(driver, navmap, settle_s=0.0)
        result = nav.capture("home", tmp_path)

        screenshot_path = Path(result["image_path"])
        actual_hash = hashlib.blake2b(screenshot_path.read_bytes()).hexdigest()
        assert result["image_blake2b"] == actual_hash


class TestCaptureManifest:
    """capture() maintains out_dir/report.json — the machine-produced evidence
    manifest (pilot finding: agent-authored reports get misplaced; Sonnet b-4)."""

    def test_first_capture_creates_the_manifest(self, tmp_path: Path) -> None:
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        result = nav.capture("home", tmp_path)

        manifest = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert result["manifest_path"] == str(tmp_path / "report.json")
        assert manifest == {
            "captures": [{"name": "home.png", "url": result["url"]}]
        }

    def test_second_capture_appends_in_order(self, tmp_path: Path) -> None:
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        nav.capture("home", tmp_path)
        nav.capture("list", tmp_path)

        manifest = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert [e["name"] for e in manifest["captures"]] == ["home.png", "list.png"]

    def test_recapture_replaces_its_entry_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        nav.capture("home", tmp_path)
        nav.capture("list", tmp_path)
        nav.capture("home", tmp_path)  # re-capture: replace, keep position

        manifest = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert [e["name"] for e in manifest["captures"]] == ["home.png", "list.png"]

    def test_malformed_existing_manifest_is_refused_not_clobbered(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "report.json").write_text("not json{", encoding="utf-8")
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        with pytest.raises(NavigationError, match="report.json"):
            nav.capture("home", tmp_path)
        # The garbage file is untouched — never silently clobber evidence.
        assert (tmp_path / "report.json").read_text(encoding="utf-8") == "not json{"

    def test_refusal_leaves_no_orphaned_screenshot(self, tmp_path: Path) -> None:
        # Manifest validation must happen BEFORE the screenshot is taken —
        # otherwise the error path itself produces evidence with no manifest
        # entry, the very bug this feature closes (inspector High, 2026-07-20).
        (tmp_path / "report.json").write_text("not json{", encoding="utf-8")
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        with pytest.raises(NavigationError):
            nav.capture("home", tmp_path)
        assert not (tmp_path / "home.png").exists()

    def test_junk_entries_in_existing_manifest_are_preserved_untouched(
        self, tmp_path: Path
    ) -> None:
        # Tolerant preservation: entries we don't understand are kept verbatim
        # (they may be someone else's evidence); ours upserts alongside them.
        junk = {"captures": ["junk", 42, {"noname": True}, None]}
        (tmp_path / "report.json").write_text(json.dumps(junk), encoding="utf-8")
        nav = Navigator(fixture_fake_driver(), fixture_navmap(), settle_s=0.0)
        nav.capture("home", tmp_path)

        manifest = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert manifest["captures"][:4] == ["junk", 42, {"noname": True}, None]
        assert manifest["captures"][4]["name"] == "home.png"


def test_goto_branching_map_executes_only_the_resolved_path() -> None:
    """Review C1 (Stage 1): goto() must SEARCH the graph purely (zero driver calls), then
    execute ONLY the resolved path. A branching node whose WRONG edge is declared FIRST must
    never produce a click on it — the original BFS physically fired every sibling edge, and
    the fixture masked it by declaring the target edge first."""
    states = (
        State(id="a", url="/a", landmarks=(Landmark(role="heading", name="A"),),
              affordances=(
                  Affordance(ref="tb", role="button", name="ToB", provenance="declared"),
                  Affordance(ref="td", role="button", name="ToD", provenance="declared"),
              ), provenance="declared"),
        State(id="b", url="/b", landmarks=(Landmark(role="heading", name="B"),),
              affordances=(), provenance="declared"),
        State(id="d", url="/d", landmarks=(Landmark(role="heading", name="D"),),
              affordances=(
                  Affordance(ref="tc", role="button", name="ToC", provenance="declared"),
              ), provenance="declared"),
        State(id="c", url="/c/:id", landmarks=(Landmark(role="heading", name="C"),),
              affordances=(), provenance="declared"),
    )
    transitions = (
        Transition(from_state="a", do={"click": "tb"}, to_state="b"),  # WRONG edge, first
        Transition(from_state="a", do={"click": "td"}, to_state="d"),
        Transition(from_state="d", do={"click": "tc"}, to_state="c"),
    )
    navmap = NavMap(app="t", states=states, transitions=transitions, flows=(),
                    raw={"version": 1, "app": "t"})
    pages = {
        "http://localhost/a": {"heading:A", "button:ToB", "button:ToD"},
        "http://localhost/b": {"heading:B"},
        "http://localhost/d": {"heading:D", "button:ToC"},
        "http://localhost/c/7": {"heading:C"},
    }
    wiring = {
        ("http://localhost/a", "button:ToB"): "http://localhost/b",
        ("http://localhost/a", "button:ToD"): "http://localhost/d",
        ("http://localhost/d", "button:ToC"): "http://localhost/c/7",
    }
    driver = FakeDriver(pages=pages, wiring=wiring)
    driver.navigate("http://localhost/a")
    nav = Navigator(driver, navmap, settle_s=0.0)
    assert nav.goto("c") == "c"
    # The no-exploration pin: the wrong sibling edge is NEVER touched.
    assert not any("ToB" in a for a in driver.actions), driver.actions
    clicks = [a for a in driver.actions if a.startswith("click")]
    assert len(clicks) == 2 and "ToD" in clicks[0] and "ToC" in clicks[1]


class TestCheckedLandmarksRuntime:
    """Checked-state landmarks in where() and goto() (Task 2, Stage 2A)."""

    def test_where_distinguishes_by_checked_landmark(self) -> None:
        """where() distinguishes two same-path states by checked landmark."""
        states = (
            State(
                id="families",
                url="/viz.html#v=families",
                landmarks=(Landmark(role="radio", name="Structure", checked=True),),
                affordances=(),
                fragment=(("v", "families"),),
                provenance="declared",
            ),
            State(
                id="globe",
                url="/viz.html#v=globe",
                landmarks=(Landmark(role="radio", name="Globe", checked=True),),
                affordances=(),
                fragment=(("v", "globe"),),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="viz", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "viz"})

        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        checked_map = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        driver = FakeDriver(pages=pages, wiring={}, checked=checked_map)
        nav = Navigator(driver, navmap, settle_s=0.0)

        # Navigate to families view with radio checked
        driver.navigate("http://localhost/viz.html#v=families")
        assert nav.where() == "families"

        # Navigate to globe view with different radio checked
        driver.navigate("http://localhost/viz.html#v=globe")
        assert nav.where() == "globe"

    def test_where_checks_landmark_checked_state(self) -> None:
        """where() requires checked state to match when declared."""
        states = (
            State(
                id="checked_state",
                url="/page",
                landmarks=(Landmark(role="radio", name="Option", checked=True),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {"http://localhost/page": {"radio:Option"}}
        checked_map = {"http://localhost/page": {"radio:Option"}}  # Option is checked
        driver = FakeDriver(pages=pages, wiring={}, checked=checked_map)
        nav = Navigator(driver, navmap, settle_s=0.0)

        driver.navigate("http://localhost/page")
        assert nav.where() == "checked_state"

        # Now unchecked
        pages_unchecked = {"http://localhost/page": {"radio:Option"}}
        driver_unchecked = FakeDriver(pages=pages_unchecked, wiring={}, checked={})
        nav_unchecked = Navigator(driver_unchecked, navmap)
        driver_unchecked.navigate("http://localhost/page")
        # Should not match because the landmark requires checked=True
        assert nav_unchecked.where() == LOST

    def test_goto_verifies_landmark_checked_state(self) -> None:
        """goto() verifies landmark checked state during navigation."""
        states = (
            State(
                id="checked_state",
                url="/page",
                landmarks=(Landmark(role="radio", name="Option", checked=True),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {"http://localhost/page": {"radio:Option"}}
        # Option is NOT checked
        driver = FakeDriver(pages=pages, wiring={}, checked={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        with pytest.raises(NavigationError, match="checked"):
            nav.goto("checked_state")


class TestPreferPathRuntime:
    """Prefer-path: declared transitions tried before direct nav (Task 3, Stage 2A)."""

    def test_broken_click_wiring_fails_param_free_target(self) -> None:
        """A param-free target with broken wiring on the declared path now FAILS."""
        states = (
            State(
                id="a",
                url="/a",
                landmarks=(Landmark(role="heading", name="A"),),
                affordances=(
                    Affordance(ref="go_b", role="button", name="GoB", provenance="declared"),
                ),
                fragment=(),
                provenance="declared",
            ),
            State(
                id="b",
                url="/b",
                landmarks=(Landmark(role="heading", name="B"),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
        )
        transitions = (
            Transition(from_state="a", do={"click": "go_b"}, to_state="b"),
        )
        navmap = NavMap(app="app", states=states, transitions=transitions, flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {
            "http://localhost/a": {"heading:A", "button:GoB"},
            "http://localhost/b": {"heading:B"},
        }
        # Wiring for GoB is MISSING — broken
        wiring = {}
        driver = FakeDriver(pages=pages, wiring=wiring)
        driver.navigate("http://localhost/a")
        nav = Navigator(driver, navmap, settle_s=0.0)

        # Now goto("b") should FAIL because the declared path is broken (no wiring)
        # (In v1, it would silently fall back to direct navigation)
        with pytest.raises(NavigationError, match="click"):
            nav.goto("b")

    def test_no_path_param_free_target_direct_nav_fallback(self) -> None:
        """Param-free target with NO declared path falls back to direct nav."""
        states = (
            State(
                id="a",
                url="/a",
                landmarks=(Landmark(role="heading", name="A"),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
            State(
                id="b",
                url="/b",
                landmarks=(Landmark(role="heading", name="B"),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
        )
        # No transitions declared
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {
            "http://localhost/a": {"heading:A"},
            "http://localhost/b": {"heading:B"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        driver.navigate("http://localhost/a")
        nav = Navigator(driver, navmap, settle_s=0.0)

        # goto("b") has no declared path, so it falls back to direct nav
        result = nav.goto("b")
        assert result == "b"
        assert driver.current_url() == "http://localhost/b"

    def test_lost_param_free_direct_nav(self) -> None:
        """LOST state with param-free target uses direct nav fallback."""
        states = (
            State(
                id="home",
                url="/",
                landmarks=(Landmark(role="heading", name="Home"),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {"http://localhost/": {"heading:Home"}}
        driver = FakeDriver(pages=pages, wiring={})
        # Don't navigate to any state, so where() returns LOST
        nav = Navigator(driver, navmap, settle_s=0.0)

        # goto("home") with LOST state should fall back to direct nav
        result = nav.goto("home")
        assert result == "home"
        assert driver.current_url() == "http://localhost/"


class TestFragmentRuntime:
    """Fragment-aware URL matching in where() and goto() (Task 1, Stage 2A)."""

    def test_where_distinguishes_by_fragment(self) -> None:
        """where() distinguishes two same-path states by declared fragment."""
        states = (
            State(
                id="families",
                url="/viz.html#v=families",
                landmarks=(Landmark(role="radio", name="Structure"),),
                affordances=(),
                fragment=(("v", "families"),),
                provenance="declared",
            ),
            State(
                id="globe",
                url="/viz.html#v=globe",
                landmarks=(Landmark(role="radio", name="Globe"),),
                affordances=(),
                fragment=(("v", "globe"),),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="viz", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "viz"})

        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
            "http://localhost/viz.html#v=globe": {"radio:Globe"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        # Navigate to families view
        driver.navigate("http://localhost/viz.html#v=families")
        assert nav.where() == "families"

        # Navigate to globe view
        driver.navigate("http://localhost/viz.html#v=globe")
        assert nav.where() == "globe"

    def test_where_fragment_subset_semantics(self) -> None:
        """where() uses subset semantics: declared pairs must match, extra live keys OK."""
        states = (
            State(
                id="view",
                url="/viz.html#v=families",
                landmarks=(Landmark(role="heading", name="View"),),
                affordances=(),
                fragment=(("v", "families"),),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="viz", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "viz"})

        # Live URL has extra fragment key 's' (e.g., scroll position)
        pages = {
            "http://localhost/viz.html#v=families&s=x": {"heading:View"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        driver.navigate("http://localhost/viz.html#v=families&s=x")
        assert nav.where() == "view"

    def test_where_no_fragment_matches_any(self) -> None:
        """where() state without declared fragment matches any live fragment."""
        states = (
            State(
                id="home",
                url="/",
                landmarks=(Landmark(role="heading", name="Home"),),
                affordances=(),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {
            "http://localhost/": {"heading:Home"},
            "http://localhost/#v=families": {"heading:Home"},
            "http://localhost/#any=fragment": {"heading:Home"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        # No fragment — matches
        driver.navigate("http://localhost/")
        assert nav.where() == "home"

        # Any fragment — still matches
        driver.navigate("http://localhost/#v=families")
        assert nav.where() == "home"

    def test_where_tie_break_fragment_specificity(self) -> None:
        """where() tie-breaks: more declared fragment pairs = more specific."""
        states = (
            State(
                id="simple",
                url="/page",
                landmarks=(Landmark(role="heading", name="Page"),),
                affordances=(),
                fragment=(),
                provenance="declared",
            ),
            State(
                id="detailed",
                url="/page#v=families&s=x",
                landmarks=(Landmark(role="heading", name="Page"),),
                affordances=(),
                fragment=(("s", "x"), ("v", "families")),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {
            "http://localhost/page": {"heading:Page"},
            "http://localhost/page#v=families&s=x": {"heading:Page"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        # When live URL has the fragment, detailed state wins (higher specificity)
        driver.navigate("http://localhost/page#v=families&s=x")
        assert nav.where() == "detailed"

        # When live URL has no fragment, simple state matches (no fragment to mismatch)
        driver.navigate("http://localhost/page")
        assert nav.where() == "simple"

    def test_goto_direct_nav_composes_fragment(self) -> None:
        """goto() composes the fragment when doing direct navigation."""
        states = (
            State(
                id="families",
                url="/viz.html#v=families",
                landmarks=(Landmark(role="radio", name="Structure"),),
                affordances=(),
                fragment=(("v", "families"),),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="viz", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "viz"})

        pages = {
            "http://localhost/viz.html#v=families": {"radio:Structure"},
        }
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        result = nav.goto("families")
        assert result == "families"
        assert driver.current_url() == "http://localhost/viz.html#v=families"

    def test_goto_param_colon_not_counted_in_fragment(self) -> None:
        """param counting ignores colons in fragment (e.g., #v=x:y doesn't add params)."""
        # A URL like "/a#v=x:y" has 0 path params (colon in fragment doesn't count)
        states = (
            State(
                id="state",
                url="/a#v=x:y",
                landmarks=(Landmark(role="heading", name="S"),),
                affordances=(),
                fragment=(("v", "x:y"),),
                provenance="declared",
            ),
        )
        navmap = NavMap(app="app", states=states, transitions=(), flows=(),
                        raw={"version": 1, "app": "app"})

        pages = {"http://localhost/a#v=x:y": {"heading:S"}}
        driver = FakeDriver(pages=pages, wiring={})
        nav = Navigator(driver, navmap, settle_s=0.0)

        # Should use direct navigation (0 params), not BFS
        result = nav.goto("state")
        assert result == "state"
        assert driver.current_url() == "http://localhost/a#v=x:y"


def test_live_fragment_parsing_is_raw_like_the_schema() -> None:
    """Review Medium (Stage 2A): parse_qsl decoded '+'/'%' on the live side while the schema
    kept declared values raw — schema-valid reserved characters silently never matched. Both
    sides now split raw. Built via parse_navmap — direct State construction skips
    _parse_fragment and made the first version of this test vacuous."""
    from cartogate.nav.schema import parse_navmap

    navmap = parse_navmap({
        "version": 1, "app": "t",
        "states": [{"id": "s", "url": "/p#v=a+b",
                    "landmarks": [{"role": "heading", "name": "S"}],
                    "affordances": [], "provenance": "declared"}],
    })
    assert navmap.states[0].fragment == (("v", "a+b"),)  # declared side is raw
    driver = FakeDriver(pages={"http://localhost/p#v=a+b": {"heading:S"}}, wiring={})
    driver.navigate("http://localhost/p#v=a+b")
    assert Navigator(driver, navmap).where() == "s"  # raw '+' matches raw '+'


def test_c1_holds_on_prefer_path_with_param_free_target() -> None:
    """Review L3 (Stage 2A): pin C1 on the NEWLY-activated code path — branching start,
    wrong edge declared first, param-free target reached via prefer-path. Only the resolved
    path may execute."""
    states = (
        State(id="a", url="/a", landmarks=(Landmark(role="heading", name="A"),),
              affordances=(
                  Affordance(ref="w", role="button", name="Wrong", provenance="declared"),
                  Affordance(ref="r", role="button", name="Right", provenance="declared"),
              ), provenance="declared"),
        State(id="b", url="/b", landmarks=(Landmark(role="heading", name="B"),),
              affordances=(), provenance="declared"),
        State(id="mid", url="/mid", landmarks=(Landmark(role="heading", name="M"),),
              affordances=(
                  Affordance(ref="g", role="button", name="Go", provenance="declared"),
              ), provenance="declared"),
        State(id="target", url="/target", landmarks=(Landmark(role="heading", name="T"),),
              affordances=(), provenance="declared"),
    )
    transitions = (
        Transition(from_state="a", do={"click": "w"}, to_state="b"),  # wrong, first
        Transition(from_state="a", do={"click": "r"}, to_state="mid"),
        Transition(from_state="mid", do={"click": "g"}, to_state="target"),
    )
    navmap = NavMap(app="t", states=states, transitions=transitions, flows=(),
                    raw={"version": 1, "app": "t"})
    driver = FakeDriver(
        pages={"http://localhost/a": {"heading:A", "button:Wrong", "button:Right"},
               "http://localhost/b": {"heading:B"},
               "http://localhost/mid": {"heading:M", "button:Go"},
               "http://localhost/target": {"heading:T"}},
        wiring={("http://localhost/a", "button:Right"): "http://localhost/mid",
                ("http://localhost/a", "button:Wrong"): "http://localhost/b",
                ("http://localhost/mid", "button:Go"): "http://localhost/target"},
    )
    driver.navigate("http://localhost/a")
    assert Navigator(driver, navmap).goto("target") == "target"
    assert not any("Wrong" in a for a in driver.actions), driver.actions


def test_direct_nav_is_base_url_agnostic() -> None:
    """Orchestrator finding (Stage 2A): Navigator hardcoded http://localhost and
    PlaywrightDriver ignored its base_url — a --base-url :8000 acceptance run would dial
    port 80. The Navigator now passes the MAP-RELATIVE url; the driver resolves it."""
    states = (
        State(id="s", url="/x", landmarks=(Landmark(role="heading", name="X"),),
              affordances=(), provenance="declared"),
    )
    navmap = NavMap(app="t", states=states, transitions=(), flows=(),
                    raw={"version": 1, "app": "t"})
    driver = FakeDriver(pages={"http://example.com:8000/x": {"heading:X"}}, wiring={},
                        base_url="http://example.com:8000")
    assert Navigator(driver, navmap).goto("s") == "s"
    assert any("example.com:8000/x" in a for a in driver.actions)
