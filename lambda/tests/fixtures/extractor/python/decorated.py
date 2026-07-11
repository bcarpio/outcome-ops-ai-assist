"""Fixture: decorated top-level defs.

Before the ADR-046 extractor fix, a ``decorated_definition`` wrapper hid the
inner def, so decorated functions/classes/methods emitted NO facts. FastAPI-style
route handlers (``@app.get``) were the common casualty.
"""

from helpers import authorize


@app.get("/items")
def list_items(req):
    return authorize(req)


@dataclass
class Record(Base):
    value: int


class Controller(Base):
    @staticmethod
    def handle(req):
        return authorize(req)

    @property
    def name(self):
        return compute_name()


def undecorated(req):
    return authorize(req)
