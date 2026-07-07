"""V8 — determinism: identical output across runs and across fresh processes.

A deterministic gate is what makes Cartogate usable as a hard block and reproducible in a
study: the same input yields byte-identical output. We assert it both within a process and
across two separate interpreters launched with different ``PYTHONHASHSEED`` (so any reliance
on set/dict iteration order would surface as a diff).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools
from cartogate.store import InMemoryStore

from .metrics import COLLECTOR

pytestmark = pytest.mark.value

FIXTURE = Path(__file__).parent / "fixtures" / "proj"

# A self-contained probe: index the fixture and dump a canonical JSON of several tool calls.
_PROBE = """
import json, sys
from pathlib import Path
from cartogate.store import InMemoryStore
from cartogate.extract.pipeline import index_package
from cartogate.mcp.tools import CartogateTools

store = InMemoryStore()
index_package(Path(sys.argv[1]), repo_id="proj", store=store)
t = CartogateTools(store)
out = {
    "find_references": t.find_references("proj.auth.validate"),
    "blast_radius": t.blast_radius("proj.auth.validate", depth=2),
    "check_duplicate": t.check_duplicate("def authenticate(name):"),
    "doc_drift": t.doc_drift(symbols=["proj.auth.authenticate"]),
    "suggest_tests": t.suggest_tests(symbols=["proj.auth.authenticate"]),
}
sys.stdout.write(json.dumps(out, sort_keys=True))
"""


def _run(hashseed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(FIXTURE)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout


def test_output_is_identical_across_processes() -> None:
    # Two fresh interpreters, deliberately different hash seeds.
    first = _run("0")
    second = _run("12345")
    assert first == second, "tool output differed across processes / hash seeds"

    # And stable within a process across repeated calls.
    tools = CartogateTools(_indexed())
    a = tools.find_references("proj.auth.validate")
    b = tools.find_references("proj.auth.validate")
    assert a == b

    COLLECTOR.record(
        hypothesis="V8",
        bucket="C",
        title="Determinism (byte-identical across processes)",
        claim="Same input yields byte-identical tool output across runs and across processes "
        "(different PYTHONHASHSEED).",
        metric={
            "cross_process_identical": True,
            "hash_seeds_compared": ["0", "12345"],
            "tools_checked": [
                "find_references",
                "blast_radius",
                "check_duplicate",
                "doc_drift",
                "suggest_tests",
            ],
            "payload_bytes": len(first),
        },
        passed=True,
        reproduce="pytest -m value tests/value/test_determinism.py",
        notes="No model, blake2b ids (unseeded), explicit output sorting → reproducible.",
    )


def _indexed() -> InMemoryStore:
    store = InMemoryStore()
    index_package(FIXTURE, repo_id="proj", store=store)
    return store
