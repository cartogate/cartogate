"""Integration tests for ``cartogate viz`` and the top-level CLI router."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from cartogate import cli
from cartogate.viz import cli as viz_cli


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "__init__.py").write_text("", "utf-8")
    (proj / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef use():\n    return add(1, 2)\n", "utf-8"
    )
    return proj


def test_viz_cli_writes_all_formats(tmp_path: Path) -> None:
    out = tmp_path / "viz"
    rc = viz_cli.main([str(_make_proj(tmp_path)), "--out-dir", str(out)])
    assert rc == 0
    assert (out / "graph.graphml").exists()
    assert (out / "graph.html").exists()
    assert (out / "graph.json").exists()

    graph = nx.parse_graphml((out / "graph.graphml").read_text(encoding="utf-8"))
    qnames = {data.get("qualified_name") for _, data in graph.nodes(data=True)}
    assert any(q.endswith(".add") for q in qnames)
    assert any(q.endswith(".use") for q in qnames)

    html = (out / "graph.html").read_text(encoding="utf-8")
    # self-contained, offline (the SVG namespace URI is an XML identifier, never fetched)
    assert "<svg" in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")


def test_viz_cli_single_format(tmp_path: Path) -> None:
    out = tmp_path / "viz"
    rc = viz_cli.main([str(_make_proj(tmp_path)), "--out-dir", str(out), "--format", "json"])
    assert rc == 0
    assert (out / "graph.json").exists()
    assert not (out / "graph.graphml").exists()


def test_top_level_cli_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "viz" in out and "daemon" in out


def test_top_level_cli_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_top_level_cli_routes_to_viz(tmp_path: Path) -> None:
    out = tmp_path / "viz"
    rc = cli.main(["viz", str(_make_proj(tmp_path)), "--out-dir", str(out), "--format", "json"])
    assert rc == 0
    assert (out / "graph.json").exists()

def test_viz_defaults_into_the_self_ignoring_state_dir(tmp_path: Path) -> None:
    """No --out-dir -> renders into <root>/.cartogate/viz, and the state dir self-ignores
    (a .gitignore containing *), so viz output never pollutes git status."""
    proj = _make_proj(tmp_path)
    rc = viz_cli.main([str(proj), "--format", "json"])
    assert rc == 0
    assert (proj / ".cartogate" / "viz" / "graph.json").exists()
    ignore = proj / ".cartogate" / ".gitignore"
    assert ignore.read_text(encoding="utf-8").strip() == "*"
