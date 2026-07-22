"""``cartogate nav`` — deterministic UI navigation checks and captures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cartogate.nav.driver import Driver
from cartogate.nav.runtime import NavigationError, Navigator
from cartogate.nav.schema import NavMapError
from cartogate.nav.schema import load as load_navmap
from cartogate.nav.testing import FakeDriver


def _load_fake_driver(fixture_path: str) -> FakeDriver:
    """Load a FakeDriver from a JSON fixture file.

    Fixture format:
    {
        "pages": {"http://...": ["role:name", ...]},
        "wiring": {"[url, target]": "http://..."}
    }

    Raises SystemExit(2) if fixture JSON is malformed.
    """
    try:
        data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError, ValueError) as exc:
        print(
            f"error: bad fake-driver fixture: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    pages_data = data.get("pages", {})
    wiring_list = data.get("wiring", {})

    # Convert pages: lists to sets
    pages: dict[str, set[str]] = {}
    for url, targets in pages_data.items():
        if isinstance(targets, list):
            pages[url] = set(targets)
        else:
            pages[url] = targets

    # Convert wiring format
    wiring: dict[tuple[str, str], str] = {}
    for key_str, dest in wiring_list.items():
        if isinstance(key_str, str):
            # Assume it's a JSON key "[url, target]"
            try:
                url, target = json.loads(key_str)
                wiring[(url, target)] = dest
            except (json.JSONDecodeError, ValueError) as exc:
                print(
                    f"error: bad fake-driver fixture: invalid wiring key {key_str!r}: {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(2) from exc
        elif isinstance(key_str, list):
            # Direct tuple as key
            wiring[tuple(key_str)] = dest

    # checked: url -> [TargetKey, ...] of elements reading as checked there (Stage 2A —
    # dropping this silently failed every checked-landmark map in browser-free mode).
    checked_data = data.get("checked", {})
    checked = {url: set(v) for url, v in checked_data.items() if isinstance(v, list)}
    return FakeDriver(pages=pages, wiring=wiring, checked=checked)


def _get_driver(driver_spec: str | None, base_url: str) -> Driver:
    """Get a driver instance from spec or default to PlaywrightDriver.

    Spec format:
      - "fake:<path.json>" → FakeDriver from fixture
      - "selenium" → SeleniumDriver (needs cartogate[nav-selenium])
      - None / default → PlaywrightDriver with base_url

    Raises SystemExit(2) if driver_spec is unrecognized.
    """
    if driver_spec:
        if driver_spec.startswith("fake:"):
            fixture_path = driver_spec.split(":", 1)[1]
            return _load_fake_driver(fixture_path)
        elif driver_spec == "selenium":
            try:
                from cartogate.nav.selenium_driver import SeleniumDriver

                return SeleniumDriver(base_url=base_url)
            except (ImportError, NavMapError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                raise SystemExit(2) from exc
        else:
            # Unrecognized driver spec
            print(
                f"error: unrecognized driver spec {driver_spec!r} — "
                f"expected 'fake:<path.json>' or omit for PlaywrightDriver",
                file=sys.stderr,
            )
            raise SystemExit(2)

    # Default to PlaywrightDriver
    try:
        from cartogate.nav.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(base_url=base_url, headless=True)
    except (ImportError, NavMapError) as exc:
        print(
            f"error: cartogate nav needs the [nav] extra — pip install 'cartogate[nav]'\n"
            f"  Details: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def check_main(args: argparse.Namespace) -> int:
    """Run ``cartogate nav check`` — verify a flow by traversing its states."""
    navmap = load_navmap(Path(args.map))
    base_url = args.base_url or "http://localhost:3000"
    driver = _get_driver(args.driver, base_url)

    try:
        # Get the flow
        if args.flow not in navmap.flows_by_name:
            print(
                f"error: flow {args.flow!r} not found in map. Available: "
                f"{', '.join(navmap.flows_by_name.keys())}",
                file=sys.stderr,
            )
            return 1

        flow = navmap.flows_by_name[args.flow]

        # Navigate through each state in the flow
        nav = Navigator(driver, navmap)
        all_passed = True

        for state_id in flow.path:
            try:
                nav.goto(state_id)
                print(f"[PASS] {state_id}")
            except NavigationError as exc:
                print(f"[FAIL] {state_id}: {exc}")
                all_passed = False
                break

        if all_passed:
            print(f"\n{args.flow}: all {len(flow.path)} states reached")
            return 0
        else:
            return 1
    finally:
        if hasattr(driver, "close"):
            driver.close()


def capture_main(args: argparse.Namespace) -> int:
    """Run ``cartogate nav capture`` — screenshot a state and return evidence bundle."""
    navmap = load_navmap(Path(args.map))
    base_url = args.base_url or "http://localhost:3000"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    driver = _get_driver(args.driver, base_url)

    try:
        # Verify state exists
        if args.state not in {s.id for s in navmap.states}:
            print(
                f"error: state {args.state!r} not found in map. Available: "
                f"{', '.join(s.id for s in navmap.states)}",
                file=sys.stderr,
            )
            return 1

        # Navigate and capture
        nav = Navigator(driver, navmap)

        try:
            bundle = nav.capture(args.state, out_dir)
        except NavigationError as exc:
            print(f"error: capture failed: {exc}", file=sys.stderr)
            return 1

        # Print bundle as JSON
        print(json.dumps(bundle))
        return 0
    finally:
        if hasattr(driver, "close"):
            driver.close()


def _discover_main(args: argparse.Namespace, driver: Driver, base_url: str) -> int:
    """Run ``cartogate nav crawl --discover`` — bounded frontier discovery."""
    from cartogate.nav.discover import DiscoveryBudget, crawl_discover

    budget = DiscoveryBudget(
        max_states=args.max_states,
        max_depth=args.max_depth,
        max_actions=args.max_actions,
        max_seconds=args.max_seconds,
    )
    try:
        report = crawl_discover(
            Path(args.map), driver, base_url=base_url, budget=budget
        )
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()

    for from_id, to_id in report.transitions:
        print(f"[EDGE] {from_id} -> {to_id}")
    for url in report.dead_ends:
        print(f"[DEAD-END] {url}")
    for hit in report.budget_hits:
        print(f"[BUDGET] stopped on {hit}")
    print(
        f"\ndiscovered {len(report.proposed_states)} new state(s) in "
        f"{report.actions} action(s); proposals written to "
        f"{report.proposed_path} — review and merge what you approve "
        "(discovered facts are never authoritative)."
    )
    if report.transitions and report.proposed_path is not None:
        # The discovered edges live in a sidecar next to the proposal — name
        # it so the user knows where to look (review 2026-07-22: the edges
        # were otherwise findable only by guessing the path).
        sidecar = report.proposed_path.with_suffix(".transitions.json")
        print(
            f"{len(report.transitions)} transition candidate(s) written to "
            f"{sidecar} — wire each in once its source state declares the "
            "affordance to click."
        )
    print(
        "note: discovery issues GET requests only (non-GET is aborted), but a "
        "link whose GET has side effects (e.g. /logout, /items/5/delete) will "
        "still execute against your dev app."
    )
    return 0


def crawl_main(args: argparse.Namespace) -> int:
    """Run ``cartogate nav crawl`` — verify+propose, or --discover frontier."""
    from cartogate.nav.crawler import crawl_verify_propose

    base_url = args.base_url or "http://localhost:3000"
    driver = _get_driver(args.driver, base_url)
    if getattr(args, "discover", False):
        try:
            return _discover_main(args, driver, base_url)
        except NavMapError as exc:
            print(f"error: {exc}", file=sys.stderr)
            close = getattr(driver, "close", None)
            if close is not None:
                close()
            return 2
    try:
        report = crawl_verify_propose(Path(args.map), driver)
    finally:
        close = getattr(driver, "close", None)
        if close is not None:
            close()

    for state_id, ok in report.verified:
        print(f"[{'PASS' if ok else 'FAIL'}] {state_id}")
    for state_id, reason in report.skipped:
        print(f"[SKIP] {state_id}: {reason}")
    print(
        f"\nvisited {len(report.visited)} state(s); proposals written to "
        f"{report.proposed_path} — review and merge what you approve "
        "(crawled facts are never authoritative)."
    )
    if not report.visited:
        print("error: no states visited — nothing verified or proposed", file=sys.stderr)
        return 1
    return 0 if all(ok for _, ok in report.verified) else 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for cartogate nav CLI."""
    parser = argparse.ArgumentParser(
        prog="cartogate nav",
        description="Deterministic UI navigation: check flows / capture states",
    )
    subparsers = parser.add_subparsers(dest="command", help="subcommand")

    # check subcommand
    check_parser = subparsers.add_parser("check", help="verify a flow by traversing states")
    check_parser.add_argument("--map", required=True, help="path to navmap.json")
    check_parser.add_argument("--flow", required=True, help="flow name to check")
    check_parser.add_argument("--base-url", help="base URL (default: http://localhost:3000)")
    check_parser.add_argument(
        "--driver",
        help="driver spec: 'fake:<fixture.json>' or default PlaywrightDriver",
    )

    # capture subcommand
    capture_parser = subparsers.add_parser("capture", help="screenshot a state")
    capture_parser.add_argument("--map", required=True, help="path to navmap.json")
    capture_parser.add_argument("--state", required=True, help="state id to capture")
    capture_parser.add_argument("--out", required=True, help="output directory for screenshot")
    capture_parser.add_argument("--base-url", help="base URL (default: http://localhost:3000)")
    capture_parser.add_argument(
        "--driver",
        help="driver spec: 'fake:<fixture.json>' or default PlaywrightDriver",
    )

    # crawl subcommand (verify+propose; --discover is a follow-up PR)
    crawl_parser = subparsers.add_parser(
        "crawl",
        help="verify declared states live; propose landmarks/affordances "
        "(crawled provenance) into <map>.proposed.json",
    )
    crawl_parser.add_argument("--map", required=True, help="path to navmap.json (drafts ok)")
    crawl_parser.add_argument("--base-url", help="base URL (default: http://localhost:3000)")
    crawl_parser.add_argument(
        "--driver",
        help="driver spec: 'fake:<fixture.json>', 'selenium', or default PlaywrightDriver",
    )
    crawl_parser.add_argument(
        "--discover",
        action="store_true",
        help="frontier discovery: propose NEW states beyond the map "
        "(PlaywrightDriver + loopback only; non-GET requests aborted — but a "
        "GET link with side effects like /logout will still fire)",
    )
    crawl_parser.add_argument("--max-states", type=int, default=30)
    crawl_parser.add_argument("--max-depth", type=int, default=5)
    crawl_parser.add_argument("--max-actions", type=int, default=200)
    crawl_parser.add_argument("--max-seconds", type=float, default=120.0)

    args = parser.parse_args(argv)

    if args.command == "check":
        return check_main(args)
    elif args.command == "capture":
        return capture_main(args)
    elif args.command == "crawl":
        return crawl_main(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
