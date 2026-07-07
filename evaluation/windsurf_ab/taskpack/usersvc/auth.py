"""Authentication and validation for users."""

from __future__ import annotations

from typing import Any

from .models import User


def validate(record: dict[str, Any]) -> bool:
    """Return True if a user record is well-formed (a non-empty ``name``)."""
    return bool(record.get("name"))


def authenticate(name: str) -> bool:
    """Return True if a user with this name is valid and may sign in."""
    return bool(name) and validate({"name": name})


def make_user(name: str) -> User:
    """Construct a :class:`User` after validating the name."""
    if not authenticate(name):
        raise ValueError("invalid user")
    return User(name)
