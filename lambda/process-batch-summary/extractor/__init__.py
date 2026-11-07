"""
Symbol-graph extractor package.

Public surface:

    extract_facts(file_path, language) -> Iterator[Fact]
    extract_facts_from_bytes(file_path, language, source) -> Iterator[Fact]

Importing this package registers every per-language extractor (Python,
TypeScript/TSX, Java, C#, Go, ABAP) via the side-effect dispatch table
in ``base.py``. The Lambda handler treats the package as a single
entry-point: it picks the language for each file (already known to the
batch pipeline) and asks for the Fact stream.

Storage, transport, and querying live elsewhere; this module's job is
strictly Fact emission. See ``docs/features.md`` (Structural retrieval)
for the end-to-end picture.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from .base import (
    DEFAULT_SYMBOL_GRAPH_FILTER,
    EXCLUDED_PATH_FRAGMENTS,
    Fact,
    SymbolGraphFilter,
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

# Side-effect: each module calls ``register_resolver()`` at import time (ADR-055).
# Languages without a resolver keep raw reference targets (no regression).
from . import python_resolver  # noqa: F401

from .resolution import build_definitions_index, resolve_repo_facts

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SYMBOL_GRAPH_FILTER",
    "EXCLUDED_PATH_FRAGMENTS",
    "Fact",
    "SymbolGraphFilter",
    "build_definitions_index",
    "extract_facts",
    "extract_facts_from_bytes",
    "is_excluded_path",
    "make_symbol_id",
    "resolve_repo_facts",
    "supported_languages",
]


def _read_source(file_path: str) -> Optional[bytes]:
    try:
        with open(file_path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("extractor cannot read %s: %s", file_path, exc)
        return None


def extract_facts(
    file_path: str,
    language: str,
    config: Optional[SymbolGraphFilter] = None,
) -> Iterator[Fact]:
    """Yield Facts for a file on disk.

    Skips the file outright when ``file_path`` is rejected by the
    per-repo ``SymbolGraphFilter`` — that filter folds in the ADR-009
    base excludes plus the repo's own include/exclude/language/size
    knobs. When ``config`` is None the module-level permissive default
    is used so legacy call sites are unchanged.
    """

    filt = config if config is not None else DEFAULT_SYMBOL_GRAPH_FILTER
    source = _read_source(file_path)
    if source is None:
        return
    if not filt.should_extract(file_path, language, len(source)):
        return
    fn = get_extractor(language)
    if fn is None:
        logger.debug("no extractor registered for language=%s", language)
        return
    yield from fn(file_path, source)


def extract_facts_from_bytes(
    file_path: str,
    language: str,
    source: bytes,
    config: Optional[SymbolGraphFilter] = None,
) -> Iterator[Fact]:
    """Yield Facts for an in-memory source buffer.

    Useful for the SQS-driven pipeline, which feeds already-fetched
    file bytes through here instead of round-tripping through the local
    filesystem. When ``config`` is None the module-level permissive
    default is used so legacy call sites are unchanged.
    """

    filt = config if config is not None else DEFAULT_SYMBOL_GRAPH_FILTER
    if not filt.should_extract(file_path, language, len(source)):
        return
    fn = get_extractor(language)
    if fn is None:
        logger.debug("no extractor registered for language=%s", language)
        return
    yield from fn(file_path, source)
