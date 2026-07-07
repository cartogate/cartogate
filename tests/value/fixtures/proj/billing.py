"""Billing — defines its OWN ``validate``, unrelated to ``auth.validate``.

This is the name-collision case: a grep for ``validate`` matches here, but billing's
``validate`` is a different symbol, so it is NOT a reference to ``auth.validate``.
"""


def charge(amount):
    return validate(amount)


def validate(amount):
    return amount > 0
