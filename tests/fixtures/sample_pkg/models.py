"""Sample module: classes + inheritance (extractor fixture)."""


class Base:
    def greet(self):
        return "hello"


class User(Base):
    def __init__(self, name):
        self.name = name
