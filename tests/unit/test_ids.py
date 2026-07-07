"""Gate tests for stable node identity (Section 1).

Identity is load-bearing: a node's id must be stable across runs and across edits to
its *body*, and must change only when an id-bearing field changes. Critically, the
content hash must NOT feed identity (risk R3) — otherwise every edit reparents the
node and the immutable-stacking model breaks.
"""

from __future__ import annotations

import inspect

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cartogate.schema import ids as ids_module
from cartogate.schema.enums import NodeKind
from cartogate.schema.ids import ID_SCHEME_VERSION, node_id


def test_node_id_is_deterministic() -> None:
    a = node_id("repoA", "pkg.mod.foo", NodeKind.SYMBOL)
    b = node_id("repoA", "pkg.mod.foo", NodeKind.SYMBOL)
    assert a == b
    assert isinstance(a, str) and len(a) > 0


def test_node_id_accepts_enum_or_str_kind() -> None:
    assert node_id("r", "q", NodeKind.SYMBOL) == node_id("r", "q", "symbol")


def test_node_id_changes_with_each_id_bearing_field() -> None:
    base = node_id("repoA", "pkg.foo", NodeKind.SYMBOL)
    assert node_id("repoB", "pkg.foo", NodeKind.SYMBOL) != base  # repo_id
    assert node_id("repoA", "pkg.bar", NodeKind.SYMBOL) != base  # qualified_name
    assert node_id("repoA", "pkg.foo", NodeKind.STATEMENT) != base  # kind
    assert node_id("repoA", "pkg.foo", NodeKind.SYMBOL, stmt_ordinal=0) != base  # ordinal


def test_stmt_ordinal_distinguishes_statements() -> None:
    s0 = node_id("r", "pkg.foo", NodeKind.STATEMENT, stmt_ordinal=0)
    s1 = node_id("r", "pkg.foo", NodeKind.STATEMENT, stmt_ordinal=1)
    assert s0 != s1


def test_id_scheme_version_participates_in_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    # The version is baked into the canonical input so a future scheme change is a
    # deliberate, detectable migration rather than a silent re-id. Bumping the version
    # must therefore change the digest for identical inputs.
    assert isinstance(ID_SCHEME_VERSION, int)
    baseline = node_id("r", "pkg.foo", NodeKind.SYMBOL)
    monkeypatch.setattr(ids_module, "ID_SCHEME_VERSION", ID_SCHEME_VERSION + 1)
    assert node_id("r", "pkg.foo", NodeKind.SYMBOL) != baseline


def test_empty_key_fields_raise() -> None:
    with pytest.raises(ValueError):
        node_id("", "pkg.foo", NodeKind.SYMBOL)
    with pytest.raises(ValueError):
        node_id("repoA", "", NodeKind.SYMBOL)


@given(
    repo=st.text(min_size=1, max_size=20),
    qname=st.text(min_size=1, max_size=40),
    ordinal=st.one_of(st.none(), st.integers(min_value=0, max_value=1000)),
)
def test_node_id_property_deterministic(repo: str, qname: str, ordinal: int | None) -> None:
    first = node_id(repo, qname, NodeKind.SYMBOL, stmt_ordinal=ordinal)
    second = node_id(repo, qname, NodeKind.SYMBOL, stmt_ordinal=ordinal)
    assert first == second


def test_node_id_signature_structurally_excludes_content() -> None:
    # The strongest possible guarantee that content cannot affect identity: there is no
    # content/body/hash parameter on node_id at all (risk R3). This is a structural check,
    # not a behavioural one — it cannot regress without changing the function signature.
    params = set(inspect.signature(node_id).parameters)
    assert params == {"repo_id", "qualified_name", "kind", "stmt_ordinal", "language"}
    assert not any("content" in p or "hash" in p or "body" in p for p in params)
