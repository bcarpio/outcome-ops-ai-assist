"""
Shared primitives for the symbol-graph extractor (chunk 01 of plan-gkg).

This module defines:

  * The ``Fact`` dataclass — the single typed record every per-language
    extractor yields.
  * Canonical symbol-ID construction helpers.
  * The repo-relative exclusion list (mirroring ADR-009's
    ``DEFAULT_EXCLUDED_PATTERNS`` so the extractor never visits a path
    the code-unit discoverer already skipped).
  * The language registry that ``extract_facts`` dispatches through.

The extractor is **emission-only** — facts are yielded; storage is the
job of chunk 02, the SQS hand-off is the job of chunk 03, and the query
layer is chunk 04. Keeping the Fact stream pure and language-agnostic
here is what makes the rest of the plan composable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional, Set


# ---------------------------------------------------------------------------
# Fact record
# ---------------------------------------------------------------------------


# ``kind`` values mirror the wire vocabulary in plan-gkg/01-symbol-extraction.md.
# Keeping them as plain strings (instead of an Enum) lets the dataclass stay
# frozen-hashable and lets downstream JSON serialization happen without a
# custom encoder.
FACT_KINDS: Set[str] = {
    "defines",
    "imports",
    "calls",
    "implements",
    "extends",
    "references",
}


@dataclass(frozen=True)
class Fact:
    """A single typed structural edge.

    Every extractor — whether Tree-sitter or regex — emits ``Fact``
    instances and nothing else. Downstream storage (chunk 02) flattens
    these onto DynamoDB items; the query API (chunk 04) reads them back.
    """

    kind: str
    source_symbol_id: str
    target_symbol_id: str
    file_path: str  # repo-relative, forward-slash normalized
    line: int  # 1-indexed
    language: str

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise ValueError(
                f"Fact.kind must be one of {sorted(FACT_KINDS)}, got {self.kind!r}"
            )
        if self.line < 1:
            raise ValueError(
                f"Fact.line must be 1-indexed (>=1), got {self.line}"
            )


# ---------------------------------------------------------------------------
# Symbol-ID construction
# ---------------------------------------------------------------------------


def make_symbol_id(
    language: str,
    module: str,
    symbol: str,
    namespace: Optional[str] = None,
) -> str:
    """Build a canonical symbol ID.

    Shape (from plan-gkg/01-symbol-extraction.md):

        ``<lang>:<package_or_module>:<class_or_namespace>?:<symbol_name>``

    The ``namespace`` segment is optional. When present, the ID has four
    colon-separated parts; when absent, three. The wildcard ``*`` is a
    valid value for ``symbol`` (used for module-level imports where the
    consumer did not pick a specific symbol).
    """

    if namespace:
        return f"{language}:{module}::{namespace}::{symbol}"
    return f"{language}:{module}::{symbol}"


# ---------------------------------------------------------------------------
# Exclusion list (parity with ADR-009 DEFAULT_EXCLUDED_PATTERNS)
# ---------------------------------------------------------------------------


# Sourced from ``lambda/process-repo-summary/backends/base.py``
# (``DEFAULT_EXCLUDED_PATTERNS``). The extractor MUST honor the same
# skip set so the code-map and the symbol-graph never diverge on which
# files they considered. Centralizing the list here also means a single
# edit propagates to every per-language extractor.
EXCLUDED_PATH_FRAGMENTS: Set[str] = {
    # Build outputs
    "target/",
    "build/",
    "dist/",
    "out/",
    "bin/",
    # Dependencies
    "node_modules/",
    "vendor/",
    ".venv/",
    "venv/",
    # IDE/tooling
    ".git/",
    ".idea/",
    ".vscode/",
    "__pycache__/",
    ".pytest_cache/",
    # Test directories (discovered separately by the test-discovery pass)
    "src/test/java/",
    "src/test/kotlin/",
    "src/test/resources/",
    "src/integrationTest/",
    "src/e2e/",
    "test-resources/",
    "testdata/",
    # Generated code
    "generated/",
    "gen/",
    # Other
    "coverage/",
}


def is_excluded_path(file_path: str) -> bool:
    """Return True when ``file_path`` matches an ADR-009 exclude fragment.

    ``file_path`` is expected to be repo-relative with forward slashes.
    Matching is by substring against the fragment, which is the shape
    used by the backend discoverer — we deliberately keep the algorithm
    identical so a future ADR-009 amendment propagates to both sides.
    """

    normalized = file_path.replace("\\", "/")
    if not normalized.endswith("/"):
        normalized_with_slash = normalized + "/"
    else:
        normalized_with_slash = normalized
    for fragment in EXCLUDED_PATH_FRAGMENTS:
        if fragment in normalized_with_slash:
            return True
    return False


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------


# Per-language extractor callable signature. Each registered extractor
# takes the repo-relative file path and the raw source as bytes (so the
# Tree-sitter extractors can hand the buffer to the parser without
# re-encoding) and yields ``Fact`` instances.
ExtractorFn = Callable[[str, bytes], Iterator[Fact]]


_REGISTRY: Dict[str, ExtractorFn] = {}


def register_language(language: str, fn: ExtractorFn) -> None:
    """Register a per-language extractor in the dispatch table."""

    _REGISTRY[language] = fn


def get_extractor(language: str) -> Optional[ExtractorFn]:
    """Return the registered extractor for ``language`` or None."""

    return _REGISTRY.get(language)


def supported_languages() -> Set[str]:
    """Return the set of currently-registered language names."""

    return set(_REGISTRY.keys())
