"""Fixture: the CALLER module for cross-module resolution tests (ADR-055).

Imports from resolve_defs and references its symbols across the file boundary,
plus one unresolvable call that must be left raw (never-guess).
"""

from resolve_defs import target_fn, Widget


def caller():
    return target_fn(5)


class SubWidget(Widget):
    def go(self):
        return undefined_helper()
