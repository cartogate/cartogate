"""Authentication helpers."""

from proj.models import User


def authenticate(name):
    """Return True if the named user is valid."""
    user = make_user(name)
    return validate(user.name)


def validate(name):
    return bool(name)


def make_user(name):
    return User(name)
