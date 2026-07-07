"""Sample module: functions, intra/cross-file calls, imports, references (fixture)."""

import os

from .models import User


def authenticate(name):
    return validate(name)


def validate(name):
    return bool(name)


def make_user(name):
    return User(name)


def pid():
    return os.getpid()


DEFAULT = User
