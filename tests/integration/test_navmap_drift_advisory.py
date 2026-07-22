"""Nav-map drift advisory (Stage 2B PR 4) — the map is told when the app moves.

Advisory-only, never affects the exit code. Fires ONLY when a staged change
removes or renames a route pattern that a checked-in ``*navmap*.json``
references in a state url — additions and unreferenced churn are silent
(noise discipline). Model-based testing died of hand-maintained-model drift;
this advisory is the freshness wire that prevents it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import git_cmd as _git
from tests.conftest import init_git_repo
from tests.conftest import write_file as _write

from cartogate.precommit import main as precommit_main


def _navmap_body(*states: tuple[str, str]) -> str:
    return json.dumps(
        {
            "version": 1,
            "app": "webapp",
            "states": [
                {
                    "id": sid,
                    "url": url,
                    "landmarks": [{"role": "heading", "name": "H"}],
                    "affordances": [],
                }
                for sid, url in states
            ],
            "transitions": [],
            "flows": [],
        }
    )


NEXT_PKG = '{"name": "t", "dependencies": {"next": "16.0.0"}}\n'


def _seed_nextjs(repo: Path) -> None:
    init_git_repo(repo)
    _write(repo, "package.json", NEXT_PKG)
    _write(repo, "app/items/[id]/page.tsx", "export default function P() {}\n")
    _write(repo, "app/about/page.tsx", "export default function A() {}\n")
    _write(
        repo, "navmap.json",
        _navmap_body(("items.id", "/items/:id"), ("about", "/about")),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed", "--no-verify")


def test_deleting_a_referenced_route_fires_the_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_nextjs(tmp_path)
    _git(tmp_path, "rm", "-q", "-r", "app/items")

    assert precommit_main([str(tmp_path)]) == 0  # advisory NEVER affects exit
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "/items/:id" in err
    assert "navmap.json" in err
    assert "items.id" in err  # the referencing state is named
    assert "ACTION:" in err


def test_deleting_an_unreferenced_route_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_nextjs(tmp_path)
    _write(tmp_path, "app/hidden/page.tsx", "export default function H() {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add hidden", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "app/hidden")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_adding_a_route_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_nextjs(tmp_path)
    _write(tmp_path, "app/new/page.tsx", "export default function N() {}\n")
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_renaming_a_referenced_route_fires_with_both_patterns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_nextjs(tmp_path)
    (tmp_path / "app" / "things").mkdir(parents=True)
    _git(tmp_path, "mv", "app/items", "app/things")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "/items/:id" in err  # the referenced (old) pattern is named


def test_react_router_pattern_removed_from_modified_file_fires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_git_repo(tmp_path)
    _write(
        tmp_path, "src/App.jsx",
        'import { Route, Routes } from "react-router-dom";\n'
        "export default function App() {\n"
        '  return <Routes><Route path="/users/:userId" element={<U />} />'
        '<Route path="/home" element={<H />} /></Routes>;\n'
        "}\n",
    )
    _write(tmp_path, "webapp.navmap.json", _navmap_body(("users", "/users/:userId")))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    _write(
        tmp_path, "src/App.jsx",
        'import { Route, Routes } from "react-router-dom";\n'
        "export default function App() {\n"
        '  return <Routes><Route path="/home" element={<H />} /></Routes>;\n'
        "}\n",
    )
    _git(tmp_path, "add", "src/App.jsx")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "/users/:userId" in err
    assert "webapp.navmap.json" in err


def test_vue_router_pattern_removed_from_modified_file_fires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_git_repo(tmp_path)
    before = (
        'import { createRouter } from "vue-router";\n'
        "export const router = createRouter({ routes: [\n"
        '  { path: "/products/:pid", component: null },\n'
        '  { path: "/home", component: null },\n'
        "] });\n"
    )
    _write(tmp_path, "src/router.ts", before)
    _write(tmp_path, "shop.navmap.json", _navmap_body(("products", "/products/:pid")))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")

    after = (
        'import { createRouter } from "vue-router";\n'
        "export const router = createRouter({ routes: [\n"
        '  { path: "/home", component: null },\n'
        "] });\n"
    )
    _write(tmp_path, "src/router.ts", after)
    _git(tmp_path, "add", "src/router.ts")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "/products/:pid" in err
    assert "shop.navmap.json" in err


def test_no_checked_in_map_means_silence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_git_repo(tmp_path)
    _write(tmp_path, "app/items/[id]/page.tsx", "export default function P() {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "app/items")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_malformed_map_never_crashes_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_git_repo(tmp_path)
    _write(tmp_path, "app/items/[id]/page.tsx", "export default function P() {}\n")
    _write(tmp_path, "navmap.json", "{not json")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "app/items")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_nested_app_tree_with_package_json_is_matched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The app/ tree nested under web/ — corroborated as a real Next.js root by
    # the sibling package.json, so the pattern is derived.
    init_git_repo(tmp_path)
    _write(tmp_path, "web/package.json", NEXT_PKG)
    _write(tmp_path, "web/app/items/[id]/page.tsx", "export default function P() {}\n")
    _write(tmp_path, "navmap.json", _navmap_body(("items.id", "/items/:id")))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "web/app/items")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "/items/:id" in err


def test_uncorroborated_app_segment_is_not_a_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # docs/app/page.tsx is NOT a Next.js tree (no package.json/next.config
    # sibling of app/) — deleting it must not fire even when a map references
    # "/" (inspector Medium: filename-coincidence false positive).
    init_git_repo(tmp_path)
    _write(tmp_path, "docs/app/page.tsx", "export default function D() {}\n")
    _write(tmp_path, "navmap.json", _navmap_body(("root", "/")))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "docs/app")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_content_only_edit_to_a_page_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_nextjs(tmp_path)
    _write(
        tmp_path, "app/items/[id]/page.tsx",
        "export default function P() { return 1; }\n",
    )
    _git(tmp_path, "add", "-A")

    assert precommit_main([str(tmp_path)]) == 0
    assert "NAVMAP DRIFT ADVISORY" not in capsys.readouterr().err


def test_fragment_bearing_map_url_still_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init_git_repo(tmp_path)
    _write(tmp_path, "package.json", NEXT_PKG)
    _write(tmp_path, "app/about/page.tsx", "export default function A() {}\n")
    _write(tmp_path, "navmap.json", _navmap_body(("about.team", "/about#tab=team")))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed", "--no-verify")
    _git(tmp_path, "rm", "-q", "-r", "app/about")

    assert precommit_main([str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "NAVMAP DRIFT ADVISORY" in err
    assert "about.team" in err
