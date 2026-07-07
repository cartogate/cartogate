"""Dead-code detection (F-67) — advisory, conservative unreferenced-internal-symbol candidates.

A top-level INTERNAL symbol that nothing in the repo references is a dead-code candidate. The
check is deliberately conservative (internal-only, top-level-only, EXTRACTED; tests/entrypoints/
dunders excluded) and NEVER blocks — dynamic dispatch / reflection / registration can still make a
flagged symbol live, so results are candidates to review.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.deadcode import find_unreferenced_internal
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore


def _index(tmp_path: Path, files: dict[str, str]) -> InMemoryStore:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        (pkg / name).write_text(body, encoding="utf-8")
    store = InMemoryStore()
    index_package(tmp_path, repo_id="pkg", store=store, base=tmp_path)
    return store


def _names(store: InMemoryStore) -> set[str]:
    return {d.qualified_name for d in find_unreferenced_internal(store)}


def test_flags_unreferenced_internal_function(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "a.py": (
                "def _helper():\n    return 1\n"  # internal, never called -> dead
                "def _used():\n    return 2\n"  # internal, called below -> not dead
                "def run():\n    return _used()\n"  # references _used; run itself is referenced? no
            ),
        },
    )
    names = _names(store)
    assert "pkg.a._helper" in names  # unreferenced internal -> flagged
    assert "pkg.a._used" not in names  # called by run -> live


def test_does_not_flag_exported_or_referenced(tmp_path: Path) -> None:
    # An exported/public symbol may be used out-of-repo, so it is never flagged even if unused.
    store = _index(
        tmp_path,
        {
            "api.py": "def public_api():\n    return _impl()\n\ndef _impl():\n    return 1\n",
        },
    )
    names = _names(store)
    assert "pkg.api._impl" not in names  # referenced by public_api -> live
    assert "pkg.api.public_api" not in names  # exported (module-public) -> never a candidate


def test_excludes_tests_and_dunders(tmp_path: Path) -> None:
    store = _index(
        tmp_path,
        {
            "test_thing.py": "def test_it():\n    assert True\n",  # test file -> excluded
            "models.py": (
                "class Thing:\n"
                "    def __init__(self):\n        self.x = 1\n"  # dunder -> excluded
            ),
        },
    )
    names = _names(store)
    assert not any("test_it" in n for n in names)  # test functions excluded
    assert not any(n.endswith("__init__") for n in names)  # dunders excluded


def test_python_callback_value_is_not_flagged(tmp_path: Path) -> None:
    # An internal function used only as a *value* (not called) is still referenced — the Python
    # walker captures the bare-name occurrence, so it must not be flagged as dead.
    store = _index(
        tmp_path,
        {"a.py": "def _fn():\n    return 1\n\n_handlers = {'x': _fn}\n"},
    )
    assert "pkg.a._fn" not in _names(store)  # used as a dict value -> live


def test_deterministic(tmp_path: Path) -> None:
    store = _index(tmp_path, {"a.py": "def _x():\n    return 1\ndef _y():\n    return 2\n"})
    first = find_unreferenced_internal(store)
    assert first == find_unreferenced_internal(store)
    assert [d.qualified_name for d in first] == sorted(d.qualified_name for d in first)
