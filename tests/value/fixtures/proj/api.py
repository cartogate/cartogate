"""Public API surface — references auth.validate and auth.authenticate."""

from proj.auth import authenticate, validate


def check(name):
    if validate(name):
        return authenticate(name)
    return None
