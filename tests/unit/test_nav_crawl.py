"""nav crawl, verify+propose mode (Stage 3) — browser-free tests.

The crawler closes the draft→usable gap: `navmap` seeds a draft the schema
refuses (landmarks are unextractable); `crawl` visits each declared state live
and PROPOSES landmarks/affordances with ``crawled`` provenance for human
approval. Proposals go to ``<map>.proposed.json`` — the live map is never
modified; merging is the human act of approval (quarantine law from the
crawler design spec).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cartogate.nav.crawler import crawl_verify_propose
from cartogate.nav.schema import NavMapError, parse_navmap
from cartogate.nav.testing import FakeDriver

DRAFT = {
    "version": 1,
    "app": "t",
    "states": [
        {"id": "root", "url": "/", "landmarks": [], "affordances": []},
        {"id": "items", "url": "/items", "landmarks": [], "affordances": []},
        {
            "id": "detail",
            "url": "/items/:id",
            "landmarks": [],
            "affordances": [],
        },
    ],
    "transitions": [],
    "flows": [],
}


def _driver() -> FakeDriver:
    pages = {
        "http://localhost/": {"heading:Welcome", "link:Browse"},
        "http://localhost/items": {"heading:Items", "link:Home"},
    }
    inventory = {
        "http://localhost/": {
            "landmarks": [{"role": "heading", "name": "Welcome"}],
            "affordances": [
                {"role": "link", "name": "Browse", "css": "[data-nav=browse]"}
            ],
        },
        "http://localhost/items": {
            "landmarks": [{"role": "heading", "name": "Items"}],
            "affordances": [{"role": "link", "name": "Home", "css": "[data-nav=home]"}],
        },
    }
    return FakeDriver(pages=pages, wiring={}, inventory=inventory)


class TestDraftParsing:
    def test_draft_mode_accepts_empty_landmarks(self) -> None:
        navmap = parse_navmap(DRAFT, draft=True)
        assert {s.id for s in navmap.states} == {"root", "items", "detail"}

    def test_strict_mode_still_refuses_the_draft(self) -> None:
        with pytest.raises(NavMapError, match="landmark"):
            parse_navmap(DRAFT)


class TestVerifyPropose:
    def test_proposals_written_to_quarantine_never_the_map(
        self, tmp_path: Path
    ) -> None:
        map_path = tmp_path / "navmap.draft.json"
        map_path.write_text(json.dumps(DRAFT), encoding="utf-8")

        report = crawl_verify_propose(map_path, _driver())

        proposed = json.loads(
            (tmp_path / "navmap.draft.proposed.json").read_text(encoding="utf-8")
        )
        by_id = {s["id"]: s for s in proposed["states"]}
        assert by_id["root"]["landmarks"] == [
            {"role": "heading", "name": "Welcome", "provenance": "crawled"}
        ]
        assert by_id["items"]["affordances"][0]["css"] == "[data-nav=home]"
        assert by_id["items"]["affordances"][0]["provenance"] == "crawled"
        # The source map file is byte-untouched:
        assert json.loads(map_path.read_text(encoding="utf-8")) == DRAFT
        assert report.visited == ["root", "items"]

    def test_paramful_states_are_skipped_and_reported(self, tmp_path: Path) -> None:
        map_path = tmp_path / "navmap.draft.json"
        map_path.write_text(json.dumps(DRAFT), encoding="utf-8")

        report = crawl_verify_propose(map_path, _driver())

        assert report.skipped == [("detail", "url has :params and no declared path")]

    def test_declared_landmarks_are_verified_not_reproposed(
        self, tmp_path: Path
    ) -> None:
        data = json.loads(json.dumps(DRAFT))
        data["states"][0]["landmarks"] = [{"role": "heading", "name": "Welcome"}]
        map_path = tmp_path / "navmap.json"
        map_path.write_text(json.dumps(data), encoding="utf-8")

        report = crawl_verify_propose(map_path, _driver())

        assert ("root", True) in report.verified
        proposed = json.loads(
            (tmp_path / "navmap.proposed.json").read_text(encoding="utf-8")
        )
        by_id = {s["id"]: s for s in proposed["states"]}
        # Declared landmarks stay declared — no crawled duplicate proposed,
        # and their provenance survives the echo (re-scan round 2 Medium):
        assert by_id["root"]["landmarks"] == [
            {"role": "heading", "name": "Welcome", "provenance": "declared"}
        ]

    def test_failed_verification_is_reported_honestly(self, tmp_path: Path) -> None:
        data = json.loads(json.dumps(DRAFT))
        data["states"][0]["landmarks"] = [{"role": "heading", "name": "NotThere"}]
        map_path = tmp_path / "navmap.json"
        map_path.write_text(json.dumps(data), encoding="utf-8")

        report = crawl_verify_propose(map_path, _driver())

        assert ("root", False) in report.verified

    def test_output_is_deterministic(self, tmp_path: Path) -> None:
        map_path = tmp_path / "navmap.draft.json"
        map_path.write_text(json.dumps(DRAFT), encoding="utf-8")
        crawl_verify_propose(map_path, _driver())
        first = (tmp_path / "navmap.draft.proposed.json").read_bytes()
        crawl_verify_propose(map_path, _driver())
        assert (tmp_path / "navmap.draft.proposed.json").read_bytes() == first


class TestFakeDriverInventory:
    def test_page_inventory_reflects_the_current_page(self) -> None:
        driver = _driver()
        driver.navigate("http://localhost/items")
        inv = driver.page_inventory()
        assert inv["landmarks"] == [{"role": "heading", "name": "Items"}]

    def test_page_inventory_empty_when_unconfigured(self) -> None:
        driver = FakeDriver(pages={"http://localhost/": set()}, wiring={})
        driver.navigate("http://localhost/")
        assert driver.page_inventory() == {"landmarks": [], "affordances": []}


class TestProposalRoundTrip:
    def test_crawler_output_parses_through_the_projects_own_schema(
        self, tmp_path: Path
    ) -> None:
        # Inspector Critical: proposed landmarks carried provenance but the
        # Landmark schema had no such key — the crawler's own primary output
        # was refused by the project's own parser. The quarantine promise is
        # "human reviews and merges"; unparseable proposals break it.
        map_path = tmp_path / "navmap.draft.json"
        map_path.write_text(json.dumps(DRAFT), encoding="utf-8")
        crawl_verify_propose(map_path, _driver())
        # Parse the REAL file verbatim — no popping "comment" (review Critical
        # 2026-07-21: masking the comment key hid that the file was unparseable).
        from cartogate.nav.schema import load

        navmap = load(tmp_path / "navmap.draft.proposed.json", draft=True)
        root = next(s for s in navmap.states if s.id == "root")
        assert root.landmarks[0].name == "Welcome"
        # The field at stake, asserted BY NAME: re-scan round 2 found the
        # parser silently dropping provenance to "declared" while this test
        # only checked .name — a test must assert the property it exists for.
        assert root.landmarks[0].provenance == "crawled"

    def test_invalid_landmark_provenance_is_refused(self) -> None:
        data = json.loads(json.dumps(DRAFT))
        data["states"][0]["landmarks"] = [
            {"role": "heading", "name": "W", "provenance": "hallucinated"}
        ]
        with pytest.raises(NavMapError, match="provenance"):
            parse_navmap(data, draft=True)

    def test_all_skipped_crawl_exits_nonzero_through_the_cli(
        self, tmp_path: Path
    ) -> None:
        # Inspector Medium: all() over empty verified was vacuously True — a
        # crawl that visited NOTHING must not report success.
        import subprocess
        import sys

        data = {
            "version": 1,
            "app": "t",
            "states": [
                {"id": "d", "url": "/x/:id", "landmarks": [], "affordances": []}
            ],
            "transitions": [],
            "flows": [],
        }
        map_path = tmp_path / "navmap.draft.json"
        map_path.write_text(json.dumps(data), encoding="utf-8")
        fixture = tmp_path / "fake.json"
        fixture.write_text(json.dumps({"pages": {}, "wiring": {}}), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, "-m", "cartogate.cli", "nav", "crawl",
                "--map", str(map_path), "--driver", f"fake:{fixture}",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "0 state(s)" in result.stdout or "visited 0" in result.stdout


class TestInventoryNamePrecedence:
    def test_label_text_outranks_control_value_in_the_shared_js(self) -> None:
        # E2E caught value-before-label proposing view KEYS instead of human
        # names; this textual pin keeps the precedence from regressing without
        # needing a browser (the E2E crawl test covers it live).
        from cartogate.nav.playwright_driver import _INVENTORY_JS

        label_pos = _INVENTORY_JS.index("label ? label.innerText")
        value_pos = _INVENTORY_JS.index("el.value")
        assert label_pos < value_pos
