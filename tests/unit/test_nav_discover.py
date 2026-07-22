"""nav crawl --discover (Stage 3 frontier discovery) — browser-free engine tests.

Discovery proposes NEW states/transitions beyond the declared map under five
controls (user decisions 2026-07-20: ships 0.7.0, budgets 30/5/200/120s, HARD loopback
refusal — no override flag). The engine is FakeDriver-testable; the non-GET
abort is a pure decision plus a live E2E.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from types import SimpleNamespace

from cartogate.nav.discover import (
    DiscoveryBudget,
    _is_loopback,
    _should_abort_method,
    _StateResolver,
    _write_transition_sidecar,
    crawl_discover,
)
from cartogate.nav.schema import NavMapError
from cartogate.nav.testing import FakeDriver

BASE = "http://localhost:8000"

SEED_MAP = {
    "version": 1,
    "app": "t",
    "states": [
        {
            "id": "home",
            "url": "/",
            "landmarks": [{"role": "heading", "name": "Home"}],
            "affordances": [],
        },
        {
            "id": "about",
            "url": "/about",
            "landmarks": [{"role": "heading", "name": "About"}],
            "affordances": [],
        },
        {
            "id": "item",
            "url": "/items/:id",
            "landmarks": [{"role": "heading", "name": "Item"}],
            "affordances": [],
        },
    ],
    "transitions": [],
    "flows": [],
}


def _app_driver() -> FakeDriver:
    """A small link-graph app: home links to about(known), docs(new),
    an item(paramful known), and an external(off-origin dead-end); docs links
    onward to docs/guide(new, depth 2)."""
    pages = {
        f"{BASE}/": {"link:About", "link:Docs", "link:Item 7", "link:External"},
        f"{BASE}/about": {"link:Home"},
        f"{BASE}/docs": {"link:Guide"},
        f"{BASE}/docs/guide": set(),
        f"{BASE}/items/7": {"link:Home"},
    }
    inventory = {
        url: {
            "landmarks": [],
            "affordances": [
                {"role": "link", "name": key.split(":", 1)[1]} for key in sorted(keys)
            ],
        }
        for url, keys in pages.items()
    }
    wiring = {
        (f"{BASE}/", "link:About"): f"{BASE}/about",
        (f"{BASE}/", "link:Docs"): f"{BASE}/docs",
        (f"{BASE}/", "link:Item 7"): f"{BASE}/items/7",
        (f"{BASE}/", "link:External"): "https://example.com/x",
        (f"{BASE}/about", "link:Home"): f"{BASE}/",
        (f"{BASE}/docs", "link:Guide"): f"{BASE}/docs/guide",
        (f"{BASE}/items/7", "link:Home"): f"{BASE}/",
    }
    # Off-origin dest is reachable so click() succeeds; discovery must NOT enqueue it.
    pages["https://example.com/x"] = set()
    inventory["https://example.com/x"] = {"landmarks": [], "affordances": []}
    return FakeDriver(pages=pages, wiring=wiring, inventory=inventory)


def _write_seed(tmp_path: Path) -> Path:
    p = tmp_path / "navmap.json"
    p.write_text(json.dumps(SEED_MAP), encoding="utf-8")
    return p


class TestPureControls:
    def test_should_abort_only_get_and_head_pass(self) -> None:
        for method in ("GET", "HEAD", "get", "head"):
            assert _should_abort_method(method) is False
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", ""):
            assert _should_abort_method(method) is True

    def test_loopback_recognition(self) -> None:
        for url in ("http://localhost:8000", "http://127.0.0.1:5173",
                    "http://[::1]:3000", "https://localhost"):
            assert _is_loopback(url) is True
        for url in ("http://example.com", "https://10.0.0.5:8000",
                    "http://evil.localhost.attacker.com"):
            assert _is_loopback(url) is False

    def test_loopback_recognizes_common_local_binds(self) -> None:
        # Widened (review 2026-07-22): 0.0.0.0 and the whole 127/8 block are
        # local dev binds; expanded IPv6 loopback is loopback. These were a
        # no-override refusal cliff for users serving on them.
        for url in ("http://0.0.0.0:8000", "http://127.0.0.2:5000",
                    "http://127.1.2.3", "http://[0:0:0:0:0:0:0:1]:3000"):
            assert _is_loopback(url) is True

    def test_loopback_fails_closed_on_userinfo_spoof(self) -> None:
        # The fail-closed guarantee: hostname is what matters, not userinfo.
        assert _is_loopback("http://127.0.0.1@evil.com/") is False
        assert _is_loopback("http://user@127.0.0.1/") is True


class TestLoopbackRefusal:
    def test_non_loopback_base_is_refused_hard(self, tmp_path: Path) -> None:
        # User decision: hard refusal, NO override flag exists.
        with pytest.raises(NavMapError, match="loopback"):
            crawl_discover(
                _write_seed(tmp_path), _app_driver(),
                base_url="http://example.com", budget=DiscoveryBudget(),
            )


class TestDiscovery:
    def test_new_same_origin_states_are_proposed(self, tmp_path: Path) -> None:
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(),
        )
        assert set(report.proposed_states) == {"docs", "docs.guide"}

    def test_known_patterns_collapse_not_reproposed(self, tmp_path: Path) -> None:
        # /items/7 collapses onto the declared /items/:id state — the graph's
        # abstraction does the equivalence work, no new state proposed.
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(),
        )
        assert "item" not in report.proposed_states
        assert ("home", "item") in report.transitions

    def test_transitions_recorded_with_state_ids(self, tmp_path: Path) -> None:
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(),
        )
        assert ("home", "about") in report.transitions
        assert ("home", "docs") in report.transitions
        assert ("docs", "docs.guide") in report.transitions

    def test_off_origin_is_a_dead_end_never_followed(self, tmp_path: Path) -> None:
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(),
        )
        assert "https://example.com/x" in report.dead_ends
        assert not any("example.com" in s for s in report.proposed_states)

    def test_proposals_quarantined_never_touch_the_map(self, tmp_path: Path) -> None:
        seed = _write_seed(tmp_path)
        before = seed.read_text(encoding="utf-8")
        crawl_discover(seed, _app_driver(), base_url=BASE, budget=DiscoveryBudget())
        assert seed.read_text(encoding="utf-8") == before
        proposed = json.loads(
            (tmp_path / "navmap.proposed.json").read_text(encoding="utf-8")
        )
        new_ids = {s["id"] for s in proposed["states"]} - {"home", "about", "item"}
        assert new_ids == {"docs", "docs.guide"}
        for s in proposed["states"]:
            if s["id"] in new_ids:
                assert all(
                    lm.get("provenance") == "crawled" for lm in s["landmarks"]
                ) or s["landmarks"] == []

    def test_output_is_deterministic(self, tmp_path: Path) -> None:
        seed = _write_seed(tmp_path)
        crawl_discover(seed, _app_driver(), base_url=BASE, budget=DiscoveryBudget())
        first = (tmp_path / "navmap.proposed.json").read_bytes()
        crawl_discover(seed, _app_driver(), base_url=BASE, budget=DiscoveryBudget())
        assert (tmp_path / "navmap.proposed.json").read_bytes() == first


REDIRECT_SEED = {
    "version": 1,
    "app": "t",
    "states": [
        {
            "id": "home",
            "url": "/",
            "landmarks": [{"role": "heading", "name": "Home"}],
            "affordances": [],
        },
        {
            "id": "secure",
            "url": "/secure",
            "landmarks": [{"role": "heading", "name": "Secure"}],
            "affordances": [],
        },
    ],
    "transitions": [],
    "flows": [],
}


def _redirect_app_driver() -> FakeDriver:
    """An app where /secure 302-redirects off-origin to an external SSO login
    (as any app with real auth does). The SSO page carries an ``Evil`` link;
    discovery must never land there, inventory it, propose it, or click it."""
    sso = "https://sso.example.com/login"
    pages = {
        f"{BASE}/": {"link:Secure"},
        sso: {"link:Evil"},
    }
    inventory = {
        f"{BASE}/": {
            "landmarks": [],
            "affordances": [{"role": "link", "name": "Secure"}],
        },
        sso: {
            "landmarks": [{"role": "heading", "name": "SSO Login"}],
            "affordances": [{"role": "link", "name": "Evil"}],
        },
    }
    wiring = {
        (f"{BASE}/", "link:Secure"): f"{BASE}/secure",
        (sso, "link:Evil"): "https://evil.example.com/pwned",
    }
    redirects = {f"{BASE}/secure": sso}
    return FakeDriver(
        pages=pages, wiring=wiring, inventory=inventory, redirects=redirects
    )


class TestStateResolver:
    def test_trailing_slash_variants_collapse_to_one_state(self) -> None:
        # /docs and /docs/ are the same page — one proposed state, not docs~2.
        seed = [SimpleNamespace(id="home", url="/")]
        resolver = _StateResolver(seed)
        first_id, first_new = resolver.resolve("http://localhost/docs")
        second_id, second_new = resolver.resolve("http://localhost/docs/")
        assert first_id == second_id
        assert first_new is True and second_new is False

    def test_root_variants_stay_root(self) -> None:
        seed = [SimpleNamespace(id="home", url="/about")]
        resolver = _StateResolver(seed)
        # "/" must not be stripped into an empty path.
        assert resolver.resolve("http://localhost/")[0] == "root"


class TestTransitionSidecar:
    def test_empty_run_removes_a_stale_sidecar(self, tmp_path: Path) -> None:
        # Cross-run dangling-edge hazard: a re-crawl yielding zero transitions
        # must not leave last run's sidecar paired with the fresh proposal.
        map_path = tmp_path / "navmap.json"
        sidecar = tmp_path / "navmap.proposed.transitions.json"
        _write_transition_sidecar(map_path, [("a", "b")])
        assert sidecar.exists()
        _write_transition_sidecar(map_path, [])
        assert not sidecar.exists()


class TestRedirectSafety:
    def test_seed_redirecting_off_origin_is_dead_end_not_proposed(
        self, tmp_path: Path
    ) -> None:
        # Release blocker (2026-07-22): the loopback promise is about the LANDED
        # origin, not just base_url. A seed state that 3xx-redirects off-origin
        # must never be inventoried, proposed as a state of THIS app, or have
        # its links clicked.
        driver = _redirect_app_driver()
        seed = tmp_path / "navmap.json"
        seed.write_text(json.dumps(REDIRECT_SEED), encoding="utf-8")
        report = crawl_discover(seed, driver, base_url=BASE, budget=DiscoveryBudget())

        assert "https://sso.example.com/login" in report.dead_ends
        # Nothing off-origin proposed (home + secure are seeds; SSO is dead-ended).
        assert report.proposed_states == []
        # The foreign page's link was never clicked.
        assert not any("Evil" in a for a in driver.actions)
        assert not any("evil.example.com" in s for s in report.proposed_states)


class TestOutputConsistency:
    def test_proposed_file_parses_through_the_schema(self, tmp_path: Path) -> None:
        # Review Critical: the proposal file must round-trip through the
        # project's own parser (comment key included) so a human can nav check.
        from cartogate.nav.schema import load

        seed = _write_seed(tmp_path)
        crawl_discover(seed, _app_driver(), base_url=BASE, budget=DiscoveryBudget())
        navmap = load(tmp_path / "navmap.proposed.json", draft=True)
        assert {"home", "about", "item", "docs", "docs.guide"} <= {
            s.id for s in navmap.states
        }

    def test_transitions_only_reference_known_states(self, tmp_path: Path) -> None:
        # Review High: a budget cut mid-crawl must not leave transitions
        # dangling to states that were never proposed/seeded.
        # max_actions=3 reaches Docs->/docs (transition + enqueue) then cuts
        # before /docs is processed — the classic dangling case.
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(max_actions=3),
        )
        assert any("max_actions" in h for h in report.budget_hits)
        known = {"home", "about", "item"} | set(report.proposed_states)
        for from_id, to_id in report.transitions:
            assert from_id in known and to_id in known


class TestBudgets:
    def test_max_states_stops_and_is_reported(self, tmp_path: Path) -> None:
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(max_states=1),
        )
        assert len(report.proposed_states) <= 1
        assert any("max_states" in h for h in report.budget_hits)

    def test_max_depth_bounds_the_frontier(self, tmp_path: Path) -> None:
        # depth 1 from seeds reaches docs but not docs/guide (depth 2).
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(max_depth=1),
        )
        assert "docs" in report.proposed_states
        assert "docs.guide" not in report.proposed_states
        assert any("max_depth" in h for h in report.budget_hits)

    def test_max_actions_caps_total_clicks(self, tmp_path: Path) -> None:
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(max_actions=2),
        )
        assert report.actions <= 2
        assert any("max_actions" in h for h in report.budget_hits)

    def test_max_seconds_stops_via_injected_clock(self, tmp_path: Path) -> None:
        ticks = iter([0.0, 0.0, 5.0, 5.0, 99.0, 99.0, 99.0, 99.0])
        report = crawl_discover(
            _write_seed(tmp_path), _app_driver(), base_url=BASE,
            budget=DiscoveryBudget(max_seconds=10.0), clock=lambda: next(ticks),
        )
        assert any("max_seconds" in h for h in report.budget_hits)
