"""Class/interface gate precision (task #24, field evidence 2026-07-04).

Per-component React ``interface Props`` and per-service ``Settings(BaseSettings)`` are
idiomatic, not duplication — a class signature (name + bases) is too weak an evidence base to
BLOCK on. The rule: **type declarations block only when the body hash also matches** (true
copy-paste); a signature-only class match never blocks. Callables (functions/methods) keep
signature-based blocking — parameter lists are real evidence, and re-implementations (different
body, same signature) are exactly what the gate exists to catch.
"""

from __future__ import annotations

from pathlib import Path

from cartogate.engine.block import BlockEngine
from cartogate.extract.pipeline import index_package
from cartogate.store import InMemoryStore
from cartogate.surfaces import find_duplicate_signatures, gate_proposed_source

NL = chr(10)


def _index(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = InMemoryStore()
    result = index_package(tmp_path, repo_id=tmp_path.name, store=store, resolve=False)
    return store, list(result.nodes)


def test_python_class_same_bases_different_body_does_not_block(tmp_path: Path) -> None:
    (tmp_path / "svc_a.py").write_text(
        "class Settings(BaseSettings):\n    redis_url: str = 'a'\n", encoding="utf-8"
    )
    store, nodes = _index(tmp_path)
    verdict = BlockEngine(store).check_duplicate(
        "Settings(BaseSettings)", proposed_body_hash="something-else"
    )
    assert verdict.blocked is False


def test_python_class_copy_paste_still_blocks(tmp_path: Path) -> None:
    body = "class Settings(BaseSettings):\n    redis_url: str = 'a'\n"
    (tmp_path / "svc_a.py").write_text(body, encoding="utf-8")
    store, nodes = _index(tmp_path)
    existing = next(n for n in nodes if n.qualified_name.endswith("svc_a.Settings"))
    assert existing.is_type_decl and existing.body_hash
    verdict = BlockEngine(store).check_duplicate(
        "Settings(BaseSettings)", proposed_body_hash=existing.body_hash
    )
    assert verdict.blocked is True  # identical body = true copy-paste


def test_function_reimplementation_still_blocks(tmp_path: Path) -> None:
    """The core gate is untouched: same signature, DIFFERENT body still blocks for callables."""
    (tmp_path / "m.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    store, nodes = _index(tmp_path)
    verdict = BlockEngine(store).check_duplicate(
        "def add(a, b)", proposed_body_hash="a-totally-different-body"
    )
    assert verdict.blocked is True


def test_commit_gate_ignores_divergent_class_groups(tmp_path: Path) -> None:
    (tmp_path / "svc_a.py").write_text(
        "class Settings(BaseSettings):\n    redis_url: str = 'a'\n", encoding="utf-8"
    )
    (tmp_path / "svc_b.py").write_text(
        "class Settings(BaseSettings):\n    chroma_path: str = 'b'\n", encoding="utf-8"
    )
    store, nodes = _index(tmp_path)
    dups = find_duplicate_signatures(nodes)
    assert dups == {}  # different bodies: idiomatic per-service Settings, not duplication


def test_commit_gate_keeps_copy_pasted_class_groups(tmp_path: Path) -> None:
    body = "class Settings(BaseSettings):\n    redis_url: str = 'a'\n"
    (tmp_path / "svc_a.py").write_text(body, encoding="utf-8")
    (tmp_path / "svc_b.py").write_text(body, encoding="utf-8")
    store, nodes = _index(tmp_path)
    dups = find_duplicate_signatures(nodes)
    assert len(dups) == 1  # byte-identical class bodies ARE copy-paste


def test_commit_gate_keeps_function_groups(tmp_path: Path) -> None:
    (tmp_path / "m1.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "m2.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    store, nodes = _index(tmp_path)
    assert len(find_duplicate_signatures(nodes)) == 1


def test_write_gate_allows_a_new_divergent_class(tmp_path: Path) -> None:
    (tmp_path / "svc_a.py").write_text(
        "class Settings(BaseSettings):\n    redis_url: str = 'a'\n", encoding="utf-8"
    )
    store, nodes = _index(tmp_path)
    proposed = "class Settings(BaseSettings):\n    chroma_path: str = 'other'\n"
    assert gate_proposed_source(store, proposed) == []


def test_write_gate_blocks_a_copy_pasted_class(tmp_path: Path) -> None:
    body = "class Settings(BaseSettings):\n    redis_url: str = 'a'\n"
    (tmp_path / "svc_a.py").write_text(body, encoding="utf-8")
    store, nodes = _index(tmp_path)
    blocked = gate_proposed_source(store, body)
    assert len(blocked) == 1 and blocked[0]["blocked"]


def test_callable_homonym_does_not_gate_a_divergent_class(tmp_path: Path) -> None:
    """THE review CRITICAL: `def Foo()` somewhere must not make a divergent `class Foo:`
    block again (cross-kind signature collision is coincidence, not evidence) — and the
    write and commit gates must AGREE."""
    (tmp_path / "a.py").write_text("def Foo():" + NL + "    return 1" + NL, encoding="utf-8")
    (tmp_path / "b.py").write_text("class Foo:" + NL + "    x = 1" + NL, encoding="utf-8")
    store, nodes = _index(tmp_path)
    proposed = "class Foo:" + NL + "    y = 2" + NL
    assert gate_proposed_source(store, proposed) == []  # write gate: not blocked
    assert find_duplicate_signatures(nodes) == {}  # commit gate agrees: no group


def test_proposed_callable_ignores_class_homonym(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("class Foo:" + NL + "    x = 1" + NL, encoding="utf-8")
    store, nodes = _index(tmp_path)
    proposed = "def Foo():" + NL + "    return 2" + NL
    assert gate_proposed_source(store, proposed) == []  # coincidence, both directions


def test_interactive_check_duplicate_returns_a_near_match_for_classes(tmp_path: Path) -> None:
    """HIGH from review: a signature-only class match must not be a silent all-clear —
    the MCP caller (no body evidence) gets the near match to inspect."""
    from cartogate.mcp.tools import CartogateTools

    (tmp_path / "svc_a.py").write_text(
        "class Settings(BaseSettings):" + NL + "    redis_url: str = 'a'" + NL,
        encoding="utf-8",
    )
    store, nodes = _index(tmp_path)
    verdict = CartogateTools(store).check_duplicate("Settings(BaseSettings)")
    assert verdict["blocked"] is False
    near = verdict["near_match"]
    assert near is not None and near["qualified_name"].endswith("svc_a.Settings")
    assert ".py:" in near["location"]


def test_blocked_verdicts_have_no_near_match(tmp_path: Path) -> None:
    from cartogate.mcp.tools import CartogateTools

    (tmp_path / "m.py").write_text(
        "def add(a, b):" + NL + "    return a + b" + NL, encoding="utf-8"
    )
    store, nodes = _index(tmp_path)
    verdict = CartogateTools(store).check_duplicate("def add(a, b)")
    assert verdict["blocked"] is True and verdict["near_match"] is None


def test_typescript_interface_copy_paste_still_blocks(tmp_path: Path) -> None:
    """Byte-identical interface bodies ARE copy-paste — and this doubles as proof that the TS
    walker records a non-None body_hash for interfaces (else this could never block)."""
    body = "export interface Props {" + chr(10) + "  title: string;" + chr(10) + "}" + chr(10)
    (tmp_path / "A.tsx").write_text(body, encoding="utf-8")
    store, nodes = _index(tmp_path)
    existing = next(n for n in nodes if n.qualified_name.endswith(".Props"))
    assert existing.is_type_decl and existing.body_hash is not None
    from cartogate.schema.enums import Language

    blocked = gate_proposed_source(store, body, Language.TYPESCRIPT)
    assert len(blocked) == 1 and blocked[0]["blocked"]


def test_typescript_interface_props_does_not_block(tmp_path: Path) -> None:
    """THE field case: a new React component's `interface Props` must not be refused."""
    (tmp_path / "A.tsx").write_text(
        "export interface Props {\n  title: string;\n}\n", encoding="utf-8"
    )
    store, nodes = _index(tmp_path)
    proposed = "export interface Props {\n  count: number;\n}\n"
    from cartogate.schema.enums import Language

    assert gate_proposed_source(store, proposed, Language.TYPESCRIPT) == []
