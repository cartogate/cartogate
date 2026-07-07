"""Domain models — ``User.close`` is a method (not a top-level symbol)."""


class User:
    def __init__(self, name):
        self.name = name

    def close(self):
        # A method named `close`; a top-level `def close()` is NOT a duplicate of it.
        return None
