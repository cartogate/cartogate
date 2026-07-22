"""Canonical hashing: the ONE source of truth for blake2b hashing.

Both contract_hash and map_hash delegate to this module. Extracted after review L1
(2026-07-19): duplicated validation logic drifting between modules caused a real bug
before (PR B: two hand-rolled check validators let a held-out check be silently
dropped) — hashing gets the same one-source-of-truth treatment preemptively.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_blake2b(raw: dict[str, Any]) -> str:
    """blake2b hex over canonical JSON (sorted keys, no whitespace) — key-order-free.

    The ONE canonical-JSON hash used by contract_hash and map_hash — do not duplicate.
    """
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(payload).hexdigest()
