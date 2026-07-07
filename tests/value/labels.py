"""Hand-labeled ground truth for the ``proj`` fixture.

These labels are written by reading the fixture source directly — **not** derived from
Cartogate's own output — so the precision/recall claims are not circular. Each label notes
the human-visible fact in the fixture it encodes.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- V3: which files (by basename) truly reference proj.auth.validate ---------------- #
# auth.authenticate calls validate; api.check calls validate; tests/test_auth.test_validate
# calls validate. billing.validate is a DIFFERENT symbol; helpers only names it in a comment.
NAV_TARGET = "proj.auth.validate"
NAV_BARE = "validate"
NAV_TRUTH_UNITS = {"auth.py", "api.py", "test_auth.py"}


# --- V4: proposed new top-level signatures, labeled is-it-a-real-duplicate ------------ #
@dataclass(frozen=True)
class DupCase:
    signature: str
    bare_name: str
    is_duplicate: bool  # human label: does an equivalent top-level callable already exist?
    note: str


DUPLICATE_CASES: tuple[DupCase, ...] = (
    DupCase("def authenticate(name):", "authenticate", True, "exact existing top-level function"),
    DupCase(
        "def authenticate(name: str) -> bool:", "authenticate", True,
        "same callable with annotations/return type — a textual compare would miss it",
    ),
    DupCase(
        "def charge(amount, currency, region):", "charge", False,
        "name exists (billing.charge) but the signature differs — not the same callable",
    ),
    DupCase(
        "def close():", "close", False,
        "`close` exists only as User.close (a method) — not a top-level duplicate",
    ),
    DupCase("def brand_new(x):", "brand_new", False, "genuinely new symbol"),
)


# --- V5: contract changes to proj.auth.authenticate, labeled breaking-vs-safe --------- #
@dataclass(frozen=True)
class ContractCase:
    label: str
    new_signature: str | None
    new_visibility: str | None  # one of "public"/"exported"/"internal" or None
    is_breaking: bool
    note: str


CONTRACT_TARGET = "proj.auth.authenticate"
CONTRACT_CASES: tuple[ContractCase, ...] = (
    ContractCase(
        "add-param", "def authenticate(name, mfa):", None, True,
        "added a required parameter — callers break",
    ),
    ContractCase(
        "narrow-visibility", None, "internal", True,
        "exported symbol made internal — importers break",
    ),
    ContractCase(
        "annotate-only", "def authenticate(name: str) -> bool:", None, False,
        "same parameters, only annotations added — safe",
    ),
    ContractCase(
        "widen-visibility", None, "public", False,
        "exported widened to public — safe",
    ),
)


# --- V6: which docs (by basename) truly document proj.auth.authenticate --------------- #
# Only README.md references it explicitly (a backtick code span). api.md / security.md use
# the English word "authenticate" incidentally — they are not documenting the symbol.
DOC_TARGET = "proj.auth.authenticate"
DOC_BARE = "authenticate"
DOC_TRUTH_UNITS = {"README.md"}


# --- V7: which tests truly exercise proj.auth.authenticate ---------------------------- #
# test_authenticate calls it directly; test_validate exercises validate, not authenticate.
TEST_TARGET = "proj.auth.authenticate"
TEST_TRUTH = {"proj.tests.test_auth.test_authenticate"}
TEST_SUITE_SIZE = 3  # test_authenticate, test_validate, test_charge
