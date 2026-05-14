"""Fixture: same function name appearing in different scopes."""


def helper():
    return "module"


class A:
    def helper(self):
        return "A"


class B:
    def helper(self):
        return "B"


def caller():
    helper()
