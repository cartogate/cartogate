"""The v0 success criterion (spec §10 Phase 0).

"Ask the agent to add a function that already exists → it calls check_duplicate, gets the
hit, reuses instead of duplicating." This test scripts that exact flow: take a proposed
snippet, pull its signatures, and confirm the gate blocks with the existing symbol.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.extract.ast_walker import extract_signatures
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "sample_pkg"


def test_adding_an_existing_function_is_blocked_with_the_existing_symbol() -> None:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    tools = CartogateTools(store)

    # The agent proposes adding a function that already exists in the package.
    proposed = "def authenticate(name):\n    return True\n"
    verdicts = [tools.check_duplicate(sig) for sig in extract_signatures(proposed)]

    blocked = [v for v in verdicts if v["blocked"]]
    assert blocked, "the duplicate should have been blocked"
    assert blocked[0]["existing_qualified_name"] == "sample_pkg.auth.authenticate"


def test_adding_a_genuinely_new_function_is_allowed() -> None:
    store = InMemoryStore()
    index_package(FIXTURE_ROOT, repo_id="t", store=store)
    tools = CartogateTools(store)

    proposed = "def compute_tax(amount, rate, region):\n    return amount * rate\n"
    verdicts = [tools.check_duplicate(sig) for sig in extract_signatures(proposed)]
    assert all(not v["blocked"] for v in verdicts)
