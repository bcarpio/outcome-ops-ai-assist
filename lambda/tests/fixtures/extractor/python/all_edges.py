"""Fixture: one file exercising every Python edge kind."""

import os
from typing import List

from helpers import normalize


class Base:
    def hello(self):
        return "hi"


class Service(Base):
    """A class with one method that calls a helper and a self method."""

    def run(self):
        normalize("x")
        self.hello()
        return os.getcwd()


def top_level():
    Service().run()
