"""Tests for cartogate.nav.schema — map schema parse/validate + self-checking map hash."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate.nav.schema import (
    Affordance,
    Flow,
    Landmark,
    NavMap,
    NavMapError,
    State,
    Transition,
    load,
    map_hash,
    parse_navmap,
)


class TestMapHash:
    """map_hash must be blake2b canonical JSON, key-order-free."""

    def test_map_hash_canonical(self) -> None:
        """Hash is deterministic over canonical JSON."""
        data = {"a": 1, "b": 2}
        h1 = map_hash(data)
        h2 = map_hash(data)
        assert h1 == h2
        assert isinstance(h1, str)
        assert len(h1) == 128  # blake2b hex = 128 chars

    def test_map_hash_key_order_free(self) -> None:
        """Hash is the same regardless of key order."""
        d1 = {"a": 1, "b": 2, "c": 3}
        d2 = {"c": 3, "a": 1, "b": 2}
        assert map_hash(d1) == map_hash(d2)

    def test_map_hash_content_sensitive(self) -> None:
        """Hash changes with content."""
        d1 = {"a": 1}
        d2 = {"a": 2}
        assert map_hash(d1) != map_hash(d2)


class TestAffordance:
    """Affordance dataclass."""

    def test_affordance_frozen(self) -> None:
        """Affordance is frozen."""
        aff = Affordance(
            ref="a1", role="button", name="Click me", css="[data-test=btn]", provenance="crawled"
        )
        with pytest.raises(AttributeError):
            aff.ref = "a2"  # type: ignore

    def test_affordance_optional_fields(self) -> None:
        """Affordance role/name/css can be None."""
        aff = Affordance(ref="a1", role="button", name=None, css=None, provenance="extracted")
        assert aff.role == "button"
        assert aff.name is None


class TestLandmark:
    """Landmark (role+name pair)."""

    def test_landmark_creation(self) -> None:
        """Landmark is a simple pair."""
        lm = Landmark(role="heading", name="Dashboard")
        assert lm.role == "heading"
        assert lm.name == "Dashboard"


class TestState:
    """State dataclass."""

    def test_state_with_landmarks(self) -> None:
        """State has id, url, landmarks tuple, affordances tuple, provenance."""
        lm = Landmark(role="heading", name="Home")
        aff = Affordance(ref="a1", role="button", name="Go", css=None, provenance="extracted")
        state = State(
            id="home",
            url="/",
            landmarks=(lm,),
            affordances=(aff,),
            provenance="extracted",
        )
        assert state.id == "home"
        assert state.url == "/"
        assert len(state.landmarks) == 1
        assert len(state.affordances) == 1

    def test_state_frozen(self) -> None:
        """State is frozen."""
        state = State(
            id="home",
            url="/",
            landmarks=(Landmark(role="heading", name="H"),),
            affordances=(),
            provenance="extracted",
        )
        with pytest.raises(AttributeError):
            state.id = "other"  # type: ignore


class TestTransition:
    """Transition dataclass."""

    def test_transition_click(self) -> None:
        """Transition with click action."""
        tr = Transition(from_state="home", do={"click": "a1"}, to_state="profile")
        assert tr.from_state == "home"
        assert tr.do == {"click": "a1"}
        assert tr.to_state == "profile"

    def test_transition_fill(self) -> None:
        """Transition with fill action."""
        tr = Transition(from_state="home", do={"fill": ["a1", "search text"]}, to_state="results")
        assert tr.do == {"fill": ["a1", "search text"]}


class TestFlow:
    """Flow dataclass."""

    def test_flow_creation(self) -> None:
        """Flow has name and path (tuple of state ids)."""
        flow = Flow(name="happy", path=("home", "profile", "settings"))
        assert flow.name == "happy"
        assert flow.path == ("home", "profile", "settings")


class TestNavMap:
    """NavMap dataclass and methods."""

    def test_navmap_creation(self) -> None:
        """NavMap has app, states, transitions, flows, raw."""
        state = State(
            id="home",
            url="/",
            landmarks=(Landmark(role="heading", name="H"),),
            affordances=(),
            provenance="extracted",
        )
        navmap = NavMap(
            app="testapp",
            states=(state,),
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp", "states": []},
        )
        assert navmap.app == "testapp"
        assert len(navmap.states) == 1

    def test_navmap_state_lookup(self) -> None:
        """NavMap.state(id) returns the state."""
        state = State(
            id="home",
            url="/",
            landmarks=(Landmark(role="heading", name="H"),),
            affordances=(),
            provenance="extracted",
        )
        navmap = NavMap(
            app="testapp",
            states=(state,),
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp", "states": []},
        )
        assert navmap.state("home") == state

    def test_navmap_state_lookup_missing(self) -> None:
        """NavMap.state() raises NavMapError for missing id."""
        navmap = NavMap(
            app="testapp",
            states=(),
            transitions=(),
            flows=(),
            raw={"version": 1, "app": "testapp", "states": []},
        )
        with pytest.raises(NavMapError, match="home"):
            navmap.state("home")

    def test_navmap_flows_by_name(self) -> None:
        """NavMap.flows_by_name returns dict of flows by name."""
        flow1 = Flow(name="happy", path=("home", "profile"))
        flow2 = Flow(name="sad", path=("home", "error"))
        navmap = NavMap(
            app="testapp",
            states=(),
            transitions=(),
            flows=(flow1, flow2),
            raw={"version": 1, "app": "testapp", "states": []},
        )
        flows_dict = navmap.flows_by_name
        assert flows_dict["happy"] == flow1
        assert flows_dict["sad"] == flow2


class TestParse:
    """parse_navmap(data) -> NavMap."""

    def test_parse_minimal_map(self) -> None:
        """Minimal valid map parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "Home"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.app == "testapp"
        assert len(navmap.states) == 1
        assert navmap.states[0].id == "home"

    def test_parse_unknown_top_level_key(self) -> None:
        """Unknown top-level key is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "match": "invalid",  # unknown key
            "states": [],
        }
        with pytest.raises(NavMapError, match="match"):
            parse_navmap(data)

    def test_parse_missing_version(self) -> None:
        """Missing version is refused."""
        data = {
            "app": "testapp",
            "states": [],
        }
        with pytest.raises(NavMapError, match="version"):
            parse_navmap(data)

    def test_parse_wrong_version(self) -> None:
        """Version != 1 is refused."""
        data = {
            "version": 2,
            "app": "testapp",
            "states": [],
        }
        with pytest.raises(NavMapError, match="version"):
            parse_navmap(data)

    def test_parse_state_without_landmarks(self) -> None:
        """State without landmarks is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [],  # invalid: needs ≥1
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="home"):
            parse_navmap(data)

    def test_parse_duplicate_state_ids(self) -> None:
        """Duplicate state ids are refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                },
                {
                    "id": "home",  # duplicate
                    "url": "/home",
                    "landmarks": [{"role": "heading", "name": "H2"}],
                    "affordances": [],
                },
            ],
        }
        with pytest.raises(NavMapError, match="home"):
            parse_navmap(data)

    def test_parse_transition_to_unknown_state(self) -> None:
        """Transition referencing unknown state is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [{"ref": "a1", "role": "button", "name": "Go"}],
                }
            ],
            "transitions": [
                {
                    "from": "home",
                    "do": {"click": "a1"},
                    "to": "unknown",  # doesn't exist
                }
            ],
        }
        with pytest.raises(NavMapError, match="unknown"):
            parse_navmap(data)

    def test_parse_transition_using_unknown_ref(self) -> None:
        """Transition referencing unknown affordance ref is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [{"ref": "a1", "role": "button", "name": "Go"}],
                }
            ],
            "transitions": [
                {
                    "from": "home",
                    "do": {"click": "a2"},  # a2 doesn't exist in home
                    "to": "home",
                }
            ],
        }
        with pytest.raises(NavMapError, match="a2"):
            parse_navmap(data)

    def test_parse_flow_referencing_unknown_state(self) -> None:
        """Flow referencing unknown state is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
            "flows": [{"name": "happy", "path": ["home", "unknown"]}],
        }
        with pytest.raises(NavMapError, match="unknown"):
            parse_navmap(data)

    def test_parse_map_hash_correct(self) -> None:
        """Correct map_hash passes self-check."""
        body = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        correct_hash = map_hash(body)
        data = {**body, "map_hash": correct_hash}
        navmap = parse_navmap(data)
        assert navmap.raw["map_hash"] == correct_hash

    def test_parse_map_hash_tampered(self) -> None:
        """Tampered map_hash is refused."""
        body = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        wrong_hash = "0" * 128  # fake hash
        data = {**body, "map_hash": wrong_hash}
        with pytest.raises(NavMapError, match="map_hash"):
            parse_navmap(data)

    def test_parse_map_hash_absent_allowed(self) -> None:
        """map_hash can be absent."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert "map_hash" not in navmap.raw


class TestLoad:
    """load(path) -> NavMap."""

    def test_load_valid_json(self, tmp_path: Path) -> None:
        """load() parses a valid JSON file."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        path = tmp_path / "map.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        navmap = load(path)
        assert navmap.app == "testapp"

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        """load() refuses invalid JSON."""
        path = tmp_path / "map.json"
        path.write_text("{ not json }", encoding="utf-8")
        with pytest.raises(NavMapError, match="JSON"):
            load(path)

    def test_load_file_not_found(self, tmp_path: Path) -> None:
        """load() raises NavMapError for missing file."""
        path = tmp_path / "missing.json"
        with pytest.raises(NavMapError):
            load(path)


class TestProvenanceValidation:
    """Provenance field must be one of: extracted, crawled, declared."""

    def test_landmark_provenance_valid_and_invalid(self) -> None:
        """Landmark provenance parses when valid, refused when not (crawler PR)."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [
                        {"role": "heading", "name": "H", "provenance": "crawled"}
                    ],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].landmarks[0].provenance == "crawled"
        data["states"][0]["landmarks"][0]["provenance"] = "guessed"
        with pytest.raises(NavMapError, match="provenance"):
            parse_navmap(data)

    def test_state_provenance_valid_extracted(self) -> None:
        """State with provenance='extracted' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                    "provenance": "extracted",
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].provenance == "extracted"

    def test_state_provenance_valid_crawled(self) -> None:
        """State with provenance='crawled' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                    "provenance": "crawled",
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].provenance == "crawled"

    def test_state_provenance_valid_declared(self) -> None:
        """State with provenance='declared' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                    "provenance": "declared",
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].provenance == "declared"

    def test_state_provenance_invalid_dict(self) -> None:
        """State with invalid provenance (dict) is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                    "provenance": {"invalid": "dict"},
                }
            ],
        }
        with pytest.raises(NavMapError, match="provenance"):
            parse_navmap(data)

    def test_state_provenance_invalid_value(self) -> None:
        """State with invalid provenance value is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                    "provenance": "wrong",
                }
            ],
        }
        with pytest.raises(NavMapError, match="provenance"):
            parse_navmap(data)

    def test_affordance_provenance_valid_extracted(self) -> None:
        """Affordance with provenance='extracted' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go", "provenance": "extracted"}
                    ],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].affordances[0].provenance == "extracted"

    def test_affordance_provenance_valid_crawled(self) -> None:
        """Affordance with provenance='crawled' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go", "provenance": "crawled"}
                    ],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].affordances[0].provenance == "crawled"

    def test_affordance_provenance_valid_declared(self) -> None:
        """Affordance with provenance='declared' parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go", "provenance": "declared"}
                    ],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.states[0].affordances[0].provenance == "declared"

    def test_affordance_provenance_invalid_value(self) -> None:
        """Affordance with invalid provenance is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go", "provenance": "invalid"}
                    ],
                }
            ],
        }
        with pytest.raises(NavMapError, match="provenance"):
            parse_navmap(data)


class TestTransitionDuplicateValidation:
    """Duplicate (from_state, canonical-do) pairs must be refused."""

    def test_duplicate_transition_same_from_and_click(self) -> None:
        """Two transitions with same from_state and click ref → refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go"},
                        {"ref": "a2", "role": "button", "name": "Go2"},
                    ],
                }
            ],
            "transitions": [
                {"from": "home", "do": {"click": "a1"}, "to": "home"},
                {"from": "home", "do": {"click": "a1"}, "to": "home"},
            ],
        }
        with pytest.raises(NavMapError, match="duplicate"):
            parse_navmap(data)

    def test_duplicate_transition_same_from_and_fill(self) -> None:
        """Two transitions with same from_state and fill ref → refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "textbox"},
                    ],
                }
            ],
            "transitions": [
                {"from": "home", "do": {"fill": ["a1", "text1"]}, "to": "home"},
                {"from": "home", "do": {"fill": ["a1", "text1"]}, "to": "home"},
            ],
        }
        with pytest.raises(NavMapError, match="duplicate"):
            parse_navmap(data)

    def test_different_ref_same_from_allowed(self) -> None:
        """Two transitions with same from_state but different refs → allowed."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [
                        {"ref": "a1", "role": "button", "name": "Go"},
                        {"ref": "a2", "role": "button", "name": "Go2"},
                    ],
                }
            ],
            "transitions": [
                {"from": "home", "do": {"click": "a1"}, "to": "home"},
                {"from": "home", "do": {"click": "a2"}, "to": "home"},
            ],
        }
        navmap = parse_navmap(data)
        assert len(navmap.transitions) == 2


class TestEntryValidation:
    """Entry field must be a valid object with url and actions."""

    def test_entry_absent_allowed(self) -> None:
        """Map without entry field parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.entry_url is None
        assert navmap.entry_actions == ()

    def test_entry_with_url_only(self) -> None:
        """Entry with just url parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": "/home"},
            "states": [
                {
                    "id": "home",
                    "url": "/home",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.entry_url == "/home"
        assert navmap.entry_actions == ()

    def test_entry_with_url_and_actions(self) -> None:
        """Entry with url and actions parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": "/home", "actions": ["init", "load"]},
            "states": [
                {
                    "id": "home",
                    "url": "/home",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        assert navmap.entry_url == "/home"
        assert navmap.entry_actions == ("init", "load")

    def test_entry_empty_url_refused(self) -> None:
        """Entry with empty url is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": ""},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry.url"):
            parse_navmap(data)

    def test_entry_missing_url_refused(self) -> None:
        """Entry without url is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"actions": []},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry.url"):
            parse_navmap(data)

    def test_entry_invalid_url_type(self) -> None:
        """Entry with non-string url is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": 123},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry.url"):
            parse_navmap(data)

    def test_entry_invalid_actions_type(self) -> None:
        """Entry with non-list actions is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": "/", "actions": "init"},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry.actions"):
            parse_navmap(data)

    def test_entry_empty_action_string_refused(self) -> None:
        """Entry with empty action string is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": "/", "actions": ["init", ""]},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry.actions"):
            parse_navmap(data)

    def test_entry_unknown_subkey_refused(self) -> None:
        """Entry with unknown subkey is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "entry": {"url": "/", "unknown": "key"},
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="entry"):
            parse_navmap(data)


def test_transition_do_must_have_exactly_one_action() -> None:
    """Re-attack residual (2026-07-19): a do with BOTH click and fill parsed, then fill was
    silently dropped at runtime — exactly one action key is now refused-or-nothing."""
    base = {
        "version": 1, "app": "t",
        "states": [
            {"id": "a", "url": "/a", "landmarks": [{"role": "heading", "name": "A"}],
             "affordances": [
                 {"ref": "r1", "role": "button", "name": "Go", "provenance": "declared"},
                 {"ref": "r2", "role": "textbox", "name": "Q", "provenance": "declared"},
             ], "provenance": "declared"},
            {"id": "b", "url": "/b", "landmarks": [{"role": "heading", "name": "B"}],
             "affordances": [], "provenance": "declared"},
        ],
    }
    both = dict(base)
    both["transitions"] = [
        {"from": "a", "do": {"click": "r1", "fill": ["r2", "x"]}, "to": "b"}]
    with pytest.raises(NavMapError, match="exactly one"):
        parse_navmap(both)
    neither = dict(base)
    neither["transitions"] = [{"from": "a", "do": {}, "to": "b"}]
    with pytest.raises(NavMapError, match="exactly one"):
        parse_navmap(neither)


class TestCheckedLandmarks:
    """Checked-state landmarks (Task 2, Stage 2A)."""

    def test_landmark_checked_accepted(self) -> None:
        """Landmark with checked=true parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "families",
                    "url": "/viz.html#v=families",
                    "landmarks": [{"role": "radio", "name": "Structure", "checked": True}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        state = navmap.states[0]
        assert len(state.landmarks) == 1
        assert state.landmarks[0].checked is True

    def test_landmark_checked_false_accepted(self) -> None:
        """Landmark with checked=false parses."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "state",
                    "url": "/page",
                    "landmarks": [{"role": "radio", "name": "Option", "checked": False}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        state = navmap.states[0]
        assert state.landmarks[0].checked is False

    def test_landmark_checked_absent_ok(self) -> None:
        """Landmark without checked field is OK (defaults to None)."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "state",
                    "url": "/page",
                    "landmarks": [{"role": "heading", "name": "Title"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        state = navmap.states[0]
        assert state.landmarks[0].checked is None

    def test_landmark_checked_non_bool_refused(self) -> None:
        """Landmark with non-bool checked is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "state",
                    "url": "/page",
                    "landmarks": [{"role": "radio", "name": "Option", "checked": "yes"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="checked"):
            parse_navmap(data)


class TestFragmentURLs:
    """Fragment-aware URLs (Task 1, Stage 2A)."""

    def test_fragment_url_parses_and_stores_sorted(self) -> None:
        """State with fragment URL stores fragment as sorted tuple of pairs."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "families",
                    "url": "/viz.html#v=families&s=x",
                    "landmarks": [{"role": "radio", "name": "Structure"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        state = navmap.states[0]
        # Fragment should be parsed and sorted by key
        assert state.fragment == (("s", "x"), ("v", "families"))

    def test_fragment_url_no_fragment_empty_tuple(self) -> None:
        """State without fragment stores empty tuple."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "Home"}],
                    "affordances": [],
                }
            ],
        }
        navmap = parse_navmap(data)
        state = navmap.states[0]
        assert state.fragment == ()

    def test_fragment_missing_equals_refused(self) -> None:
        """Fragment with segment missing '=' is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "bad",
                    "url": "/viz.html#v=families&noeq",
                    "landmarks": [{"role": "heading", "name": "Bad"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="fragment"):
            parse_navmap(data)

    def test_fragment_empty_key_refused(self) -> None:
        """Fragment with empty key is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "bad",
                    "url": "/viz.html#=value",
                    "landmarks": [{"role": "heading", "name": "Bad"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="fragment"):
            parse_navmap(data)

    def test_fragment_empty_value_refused(self) -> None:
        """Fragment with empty value is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "bad",
                    "url": "/viz.html#v=",
                    "landmarks": [{"role": "heading", "name": "Bad"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="fragment"):
            parse_navmap(data)

    def test_fragment_duplicate_key_refused(self) -> None:
        """Fragment with duplicate keys is refused."""
        data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "bad",
                    "url": "/viz.html#v=families&v=globe",
                    "landmarks": [{"role": "heading", "name": "Bad"}],
                    "affordances": [],
                }
            ],
        }
        with pytest.raises(NavMapError, match="duplicate"):
            parse_navmap(data)
