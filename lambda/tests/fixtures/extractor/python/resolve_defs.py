"""Fixture: the DEFINING module for cross-module resolution tests (ADR-055)."""


def target_fn(x):
    return x


class Widget:
    def use(self):
        return 1
