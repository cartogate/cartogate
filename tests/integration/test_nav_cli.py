"""Integration tests for cartogate nav CLI (check/capture commands)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from cartogate.nav.schema import NavMapError
from cartogate.nav_cli import main


class TestNavCheckCLI:
    """cartogate nav check command."""

    def test_check_happy_via_fake_driver(
        self, tmp_path: Path
    ) -> None:
        """check command via fake driver fixture."""
        # Create a simple map
        map_data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "Home"}],
                    "affordances": [{"ref": "a1", "role": "button", "name": "Go"}],
                },
                {
                    "id": "page2",
                    "url": "/page2",
                    "landmarks": [{"role": "heading", "name": "Page 2"}],
                    "affordances": [],
                },
            ],
            "transitions": [{"from": "home", "do": {"click": "a1"}, "to": "page2"}],
            "flows": [{"name": "happy", "path": ["home", "page2"]}],
        }

        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Create a fake driver fixture
        fake_pages = {
            "http://localhost/": ["heading:Home", "button:Go"],
            "http://localhost/page2": ["heading:Page 2"],
        }
        fake_wiring = {
            json.dumps(["http://localhost/", "button:Go"]): "http://localhost/page2",
        }
        fixture_data = {
            "pages": fake_pages,
            "wiring": fake_wiring,
        }
        fixture_path = tmp_path / "fake-fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        # Run the check command
        result = subprocess.run(
            [
                "python",
                "-m",
                "cartogate.nav_cli",
                "check",
                "--map",
                str(map_path),
                "--flow",
                "happy",
                "--driver",
                f"fake:{fixture_path}",
            ],
            capture_output=True,
            text=True,
        )

        # Should succeed
        assert result.returncode == 0
        assert "PASS" in result.stdout or "home" in result.stdout


class TestNavCaptureCLI:
    """cartogate nav capture command."""

    def test_capture_via_fake_driver(
        self, tmp_path: Path
    ) -> None:
        """capture command via fake driver fixture."""
        # Create a simple map
        map_data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "Home"}],
                    "affordances": [],
                },
            ],
        }

        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Create a fake driver fixture
        fake_pages = {
            "http://localhost/": ["heading:Home"],
        }
        fixture_data = {
            "pages": fake_pages,
            "wiring": {},
        }
        fixture_path = tmp_path / "fake-fixture.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        # Run the capture command
        result = subprocess.run(
            [
                "python",
                "-m",
                "cartogate.nav_cli",
                "capture",
                "--map",
                str(map_path),
                "--state",
                "home",
                "--out",
                str(out_dir),
                "--driver",
                f"fake:{fixture_path}",
            ],
            capture_output=True,
            text=True,
        )

        # Should succeed and print JSON to stdout
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = result.stdout.strip()
        # Parse the JSON output
        bundle = json.loads(output)
        assert bundle["state"] == "home"
        assert "map_hash" in bundle
        assert "image_path" in bundle
        assert Path(bundle["image_path"]).exists()
        # The evidence manifest is machine-produced in --out (Sonnet b-4 seam):
        manifest = json.loads(
            (out_dir / "report.json").read_text(encoding="utf-8")
        )
        assert manifest["captures"] == [
            {"name": "home.png", "url": "http://localhost/"}
        ]
        assert bundle["manifest_path"] == str(out_dir / "report.json")


class TestNavCliPlaywrightGuard:
    """Playwright guard: missing [nav] extra should exit cleanly."""

    def test_check_missing_playwright_exits_2(
        self, tmp_path: Path
    ) -> None:
        """check command without playwright and no --driver exits 2 with message."""
        # Create a simple map
        map_data = {
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

        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Monkeypatch require_playwright to raise NavMapError
        with mock.patch(
            "cartogate.nav.playwright_driver.require_playwright"
        ) as mock_require:
            mock_require.side_effect = NavMapError(
                "cartogate nav needs the [nav] extra — pip install 'cartogate[nav]'"
            )
            # Call main() with check command (no --driver, so it tries Playwright)
            # main() raises SystemExit on error
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "check",
                        "--map",
                        str(map_path),
                        "--flow",
                        "nonexistent",  # doesn't matter, error before checking flow
                    ]
                )
            # Should exit with code 2
            assert exc_info.value.code == 2


class TestNavCliDriverValidation:
    """Driver spec validation: unrecognized --driver value must error."""

    def test_unrecognized_driver_value_exits_2(
        self, tmp_path: Path
    ) -> None:
        """check command exits 2 with message if --driver value is unrecognized."""
        map_data = {
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
            "flows": [{"name": "happy", "path": ["home"]}],
        }
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Run check command with unrecognized driver value
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "check",
                    "--map",
                    str(map_path),
                    "--flow",
                    "happy",
                    "--driver",
                    "unknown:something",  # Not 'fake:' or anything recognized
                ]
            )
        # Should exit 2
        assert exc_info.value.code == 2


class TestNavCliFixtureParsing:
    """Fixture parsing errors must exit cleanly with error message."""

    def test_malformed_fixture_json_exits_2(
        self, tmp_path: Path
    ) -> None:
        """check command exits 2 with message if fake fixture JSON is invalid."""
        map_data = {
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
            "flows": [{"name": "happy", "path": ["home"]}],
        }
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Create a malformed fixture JSON
        fixture_path = tmp_path / "bad-fixture.json"
        fixture_path.write_text("{ not valid json }", encoding="utf-8")

        # Run check command with malformed fixture
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "check",
                    "--map",
                    str(map_path),
                    "--flow",
                    "happy",
                    "--driver",
                    f"fake:{fixture_path}",
                ]
            )
        # Should exit 2
        assert exc_info.value.code == 2


class TestNavCliDriverCleanup:
    """Driver cleanup: close() must be called even on error."""

    def test_check_driver_close_called_on_success(
        self, tmp_path: Path
    ) -> None:
        """check command calls driver.close() on success."""
        map_data = {
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
            "flows": [{"name": "happy", "path": ["home"]}],
        }
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Use a fake driver with a spy close() method
        fake_pages = {"http://localhost/": ["heading:Home"]}
        fixture_data = {"pages": fake_pages, "wiring": {}}
        fixture_path = tmp_path / "fake.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        # Monkeypatch FakeDriver to track close() calls
        from cartogate.nav.testing import FakeDriver as OrigFakeDriver

        class SpyFakeDriver(OrigFakeDriver):
            close_called = False

            def close(self) -> None:
                SpyFakeDriver.close_called = True
                # FakeDriver doesn't need cleanup, but we track that close was called

        with mock.patch("cartogate.nav_cli.FakeDriver", SpyFakeDriver):
            exit_code = main(
                [
                    "check",
                    "--map",
                    str(map_path),
                    "--flow",
                    "happy",
                    "--driver",
                    f"fake:{fixture_path}",
                ]
            )
            assert exit_code == 0
            # close() should have been called
            assert SpyFakeDriver.close_called

    def test_check_driver_close_called_on_flow_error(
        self, tmp_path: Path
    ) -> None:
        """check command calls driver.close() even when flow fails."""
        map_data = {
            "version": 1,
            "app": "testapp",
            "states": [
                {
                    "id": "home",
                    "url": "/",
                    "landmarks": [{"role": "heading", "name": "Home"}],
                    "affordances": [],
                },
                {
                    "id": "other",
                    "url": "/other",
                    "landmarks": [{"role": "heading", "name": "Other"}],
                    "affordances": [],
                },
            ],
            "flows": [{"name": "happy", "path": ["home", "other"]}],
        }
        map_path = tmp_path / "map.json"
        map_path.write_text(json.dumps(map_data), encoding="utf-8")

        # Use a fake driver with /other but missing the landmark (verification will fail)
        fake_pages = {
            "http://localhost/": ["heading:Home"],
            "http://localhost/other": [],  # No landmarks, verification will fail
        }
        fixture_data = {"pages": fake_pages, "wiring": {}}
        fixture_path = tmp_path / "fake.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        # Monkeypatch FakeDriver to track close() calls
        from cartogate.nav.testing import FakeDriver as OrigFakeDriver

        class SpyFakeDriver(OrigFakeDriver):
            close_called = False

            def close(self) -> None:
                SpyFakeDriver.close_called = True
                # FakeDriver doesn't need cleanup, but we track that close was called

        with mock.patch("cartogate.nav_cli.FakeDriver", SpyFakeDriver):
            exit_code = main(
                [
                    "check",
                    "--map",
                    str(map_path),
                    "--flow",
                    "happy",
                    "--driver",
                    f"fake:{fixture_path}",
                ]
            )
            # Should fail (can't navigate to /other)
            assert exit_code == 1
            # close() should still have been called
            assert SpyFakeDriver.close_called


def test_fake_driver_fixture_carries_checked_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review High (Stage 2A): the fake: fixture loader must pass the checked sets through —
    dropping them made every checked-landmark map fail browser-free CLI checks."""
    navmap = {
        "version": 1, "app": "t",
        "states": [
            {"id": "fam", "url": "/v.html#v=families",
             "landmarks": [{"role": "radio", "name": "Structure", "checked": True}],
             "affordances": [], "provenance": "declared"},
        ],
        "flows": [{"name": "solo", "path": ["fam"]}],
    }
    m = tmp_path / "m.json"
    m.write_text(json.dumps(navmap), encoding="utf-8")
    fixture = {
        "pages": {"http://localhost/v.html#v=families": ["radio:Structure"]},
        "wiring": {},
        "checked": {"http://localhost/v.html#v=families": ["radio:Structure"]},
    }
    f = tmp_path / "f.json"
    f.write_text(json.dumps(fixture), encoding="utf-8")
    rc = main(["check", "--map", str(m), "--flow", "solo", "--driver", f"fake:{f}"])
    assert rc == 0, capsys.readouterr().err
