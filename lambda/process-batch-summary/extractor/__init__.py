"""
Symbol-graph extractor package (plan-gkg chunk 01).

Public surface:

    extract_facts(file_path, language) -> Iterator[Fact]
    extract_facts_from_bytes(file_path, language, source) -> Iterator[Fact]

Importing this package registers every per-language extractor (Python,
TypeScript/TSX, Java, C#, Go, ABAP) via the side-effect dispatch table
in ``base.py``. The Lambda handler treats the package as a single
entry-point: it picks the language for each file (already known to the
batch pipeline) and asks for the Fact stream.

Storage, transport, and querying are NOT this chunk's concern — see
chunks 02-04 of plan-gkg/.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from .base import (
    Fact,
    EXCLUDED_PATH_FRAGMENTS,
    get_extractor,
    is_excluded_path,
    make_symbol_id,
    supported_languages,
)

# Side-effect: each module calls ``register_language()`` at import time.
from . import python_extractor  # noqa: F401
from . import typescript_extractor  # noqa: F401
from . import java_extractor  # noqa: F401
from . import csharp_extractor  # noqa: F401
from . import go_extractor  # noqa: F401
from . import abap_extractor  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = [
    "Fact",
    "EXCLUDED_PATH_FRAGMENTS",
    "extract_facts",
    "extract_facts_from_bytes",
    "is_excluded_path",
    "make_symbol_id",
    "supported_languages",
]


def _read_source(file_path: str) -> Optional[bytes]:
    try:
        with open(file_path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("extractor cannot read %s: %s", file_path, exc)
        return None


def extract_facts(file_path: str, language: str) -> Iterator[Fact]:
    """Yield Facts for a file on disk.

    Skips the file outright when ``file_path`` matches an ADR-009
    exclude fragment so the extractor never disagrees with the
    code-unit discoverer about which files are in scope.
    """

    if is_excluded_path(file_path):
        return
    fn = get_extractor(language)
    if fn is None:
        logger.debug("no extractor registered for language=%s", language)
        return
    source = _read_source(file_path)
    if source is None:
        return
    yield from fn(file_path, source)


def extract_facts_from_bytes(
    file_path: str, language: str, source: bytes
) -> Iterator[Fact]:
    """Yield Facts for an in-memory source buffer.

    Useful for the SQS-driven pipeline (chunk 03 will feed already-
    fetched file bytes through here instead of round-tripping through
    the local filesystem).
    """

    if is_excluded_path(file_path):
        return
    fn = get_extractor(language)
    if fn is None:
        logger.debug("no extractor registered for language=%s", language)
        return
    yield from fn(file_path, source)
