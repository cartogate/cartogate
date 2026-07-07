"""Tests for the daemon wire protocol (newline-delimited JSON)."""

from __future__ import annotations

import pytest

from cartogate.daemon.protocol import (
    ProtocolError,
    build_error,
    build_ok,
    build_request,
    decode,
    encode,
)


def test_encode_is_one_json_line() -> None:
    raw = encode({"a": 1})
    assert raw.endswith(b"\n")
    assert b"\n" not in raw[:-1]  # exactly one line


def test_encode_decode_round_trip() -> None:
    msg = build_request("tok", "check_duplicate", {"signature": "def f(x):"})
    assert decode(encode(msg)) == msg


def test_request_shape() -> None:
    req = build_request("tok", "check_duplicate", {"signature": "x"})
    assert req == {"token": "tok", "tool": "check_duplicate", "arguments": {"signature": "x"}}


def test_ok_and_error_shapes() -> None:
    assert build_ok({"blocked": True}) == {"ok": True, "result": {"blocked": True}}
    err = build_error("nope")
    assert err == {"ok": False, "error": "nope"}


def test_decode_rejects_malformed() -> None:
    with pytest.raises(ProtocolError):
        decode(b"not json\n")
    with pytest.raises(ProtocolError):
        decode(b"[1, 2, 3]\n")  # not an object
