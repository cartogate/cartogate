"""Domain models for the user service."""

from __future__ import annotations


class User:
    """A user of the system, identified by name."""

    def __init__(self, name: str) -> None:
        self.name = name

    def greet(self) -> str:
        return f"hi {self.name}"
