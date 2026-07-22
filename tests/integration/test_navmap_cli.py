"""``cartogate navmap`` — seed export from extracted ROUTE nodes (Stage 2B PR 3).

The seed is a DRAFT on purpose: states need >=1 landmark to be schema-valid,
and landmarks are unextractable from routes — fabricating placeholders would
violate extraction honesty, so the draft inherits the schema's refusal until
a human fills them in. Suggestions (links_to-derived transitions) live in a
separate sidecar file; the map schema's unknown-key refusal stays intact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cartogate.nav.schema import NavMapError, parse_navmap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "routes"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cartogate.cli", "navmap", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


class TestNavmapSeed:
    def test_draft_contains_one_state_per_route(self, tmp_path: Path) -> None:
        out = tmp_path / "navmap.draft.json"
        result = _run(str(FIXTURES / "react-router"), "--out", str(out), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        draft = json.loads(out.read_text(encoding="utf-8"))
        assert draft["version"] == 1
        assert {s["id"]: s["url"] for s in draft["states"]} == {
            "root": "/",
            "users.userId": "/users/:userId",
            "settings": "/settings",
        }

    def test_draft_is_refused_by_the_schema_until_landmarks_are_filled(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "navmap.draft.json"
        _run(str(FIXTURES / "react-router"), "--out", str(out), cwd=tmp_path)
        draft = json.loads(out.read_text(encoding="utf-8"))
        # The refusal IS the feature: an unverifiable map must not parse.
        with pytest.raises(NavMapError, match="landmark"):
            parse_navmap(draft)

    def test_draft_plus_landmarks_round_trips(self, tmp_path: Path) -> None:
        out = tmp_path / "navmap.draft.json"
        _run(str(FIXTURES / "react-router"), "--out", str(out), cwd=tmp_path)
        draft = json.loads(out.read_text(encoding="utf-8"))
        for state in draft["states"]:
            state["landmarks"] = [{"role": "heading", "name": "Filled"}]
        navmap = parse_navmap(draft)
        assert {s.id for s in navmap.states} == {"root", "users.userId", "settings"}

    def test_suggestions_sidecar_from_links_to(self, tmp_path: Path) -> None:
        out = tmp_path / "navmap.draft.json"
        result = _run(str(FIXTURES / "nextjs-app"), "--out", str(out), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        sidecar = json.loads(
            (tmp_path / "navmap.draft.suggestions.json").read_text(encoding="utf-8")
        )
        assert {
            (s["from"], s["to"]) for s in sidecar["suggested_transitions"]
        } == {("root", "items"), ("items", "items.id")}

    def test_output_is_deterministic(self, tmp_path: Path) -> None:
        out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
        _run(str(FIXTURES / "nextjs-app"), "--out", str(out1), cwd=tmp_path)
        _run(str(FIXTURES / "nextjs-app"), "--out", str(out2), cwd=tmp_path)
        assert out1.read_bytes() == out2.read_bytes()

    def test_app_flag_and_stderr_guidance(self, tmp_path: Path) -> None:
        out = tmp_path / "navmap.draft.json"
        result = _run(
            str(FIXTURES / "react-router"), "--out", str(out), "--app", "myapp",
            cwd=tmp_path,
        )
        draft = json.loads(out.read_text(encoding="utf-8"))
        assert draft["app"] == "myapp"
        # The CLI names exactly what a human must fill in.
        assert "landmark" in (result.stdout + result.stderr).lower()

    def test_colliding_state_ids_are_disambiguated_and_reported(
        self, tmp_path: Path
    ) -> None:
        # "/a/b" and "/a.b" both naively map to id "a.b" (inspector Medium):
        # the draft must not contain duplicate ids, and the CLI must name both
        # source patterns so the human can rename meaningfully.
        src = tmp_path / "proj" / "src"
        src.mkdir(parents=True)
        (src / "App.jsx").write_text(
            'import { Route, Routes } from "react-router-dom";\n'
            "export default function App() {\n"
            "  return (\n"
            "    <Routes>\n"
            '      <Route path="/a/b" element={<X />} />\n'
            '      <Route path="/a.b" element={<Y />} />\n'
            "    </Routes>\n"
            "  );\n"
            "}\n",
            encoding="utf-8",
        )
        out = tmp_path / "navmap.draft.json"
        result = _run(str(tmp_path / "proj"), "--out", str(out), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        draft = json.loads(out.read_text(encoding="utf-8"))
        ids = [s["id"] for s in draft["states"]]
        assert len(ids) == len(set(ids)), f"duplicate state ids: {ids}"
        text = result.stdout + result.stderr
        assert "/a/b" in text and "/a.b" in text  # both patterns named

    def test_no_sidecar_when_no_suggestions(self, tmp_path: Path) -> None:
        out = tmp_path / "navmap.draft.json"
        _run(str(FIXTURES / "react-router"), "--out", str(out), cwd=tmp_path)
        # App.jsx declares two routes -> its links have no unique source, so
        # zero suggestions -> the sidecar must not exist at all.
        assert not (tmp_path / "navmap.draft.suggestions.json").exists()

    def test_out_parent_dirs_are_created(self, tmp_path: Path) -> None:
        out = tmp_path / "deep" / "nested" / "navmap.draft.json"
        result = _run(str(FIXTURES / "react-router"), "--out", str(out), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert out.exists()

    def test_no_routes_found_exits_1_with_message(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        out = tmp_path / "navmap.draft.json"
        result = _run(str(empty), "--out", str(out), cwd=tmp_path)
        assert result.returncode == 1
        assert "no route" in (result.stdout + result.stderr).lower()
        assert not out.exists()
