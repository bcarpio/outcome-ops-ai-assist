"""
Shared primitives for the symbol-graph extractor.

This module defines:

  * The ``Fact`` dataclass — the single typed record every per-language
    extractor yields.
  * Canonical symbol-ID construction helpers.
  * The repo-relative exclusion list (mirroring ADR-009's
    ``DEFAULT_EXCLUDED_PATTERNS`` so the extractor never visits a path
    the code-unit discoverer already skipped).
  * The language registry that ``extract_facts`` dispatches through.

The extractor is **emission-only** — facts are yielded; storage,
the SQS hand-off, and the query layer all live in separate modules.
Keeping the Fact stream pure and language-agnostic here is what makes
the surrounding pipeline composable.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Fact record
# ---------------------------------------------------------------------------


# ``kind`` values are the fixed wire vocabulary of the symbol-graph Fact
# stream. Keeping them as plain strings (instead of an Enum) lets the
# dataclass stay frozen-hashable and lets downstream JSON serialization
# happen without a custom encoder.
FACT_KINDS: Set[str] = {
    "defines",
    "imports",
    "calls",
    "implements",
    "extends",
    "references",
}


@dataclass(frozen=True)
class Param:
    """One parameter on a callable's signature.

    ``type`` is the declared/annotated type when the language provides one
    and the parser sees it (Python annotations, TS/Java/C# types, ABAP
    form params) — otherwise ``None``. Never inferred. Consistent with
    ADR-027: capture what the parser sees, never paraphrase.
    """

    name: str
    type: Optional[str] = None


@dataclass(frozen=True)
class Signature:
    """Parameter list + optional return type for a callable.

    Rides on the ``defines`` fact of the callable it documents.
    ``params`` is a tuple (not list) so the enclosing Fact stays frozen-
    hashable. Order is source order. ``returns`` is the declared return
    type or ``None`` when absent / not annotated.
    """

    params: Tuple[Param, ...] = ()
    returns: Optional[str] = None


@dataclass(frozen=True)
class Fact:
    """A single typed structural edge.

    Every extractor — whether Tree-sitter or regex — emits ``Fact``
    instances and nothing else. Downstream storage flattens these onto
    DynamoDB items; the query API reads them back.

    ``signature`` is populated only on ``kind == "defines"`` facts for
    callables (functions / methods). Other kinds and non-callable
    defines (classes, variables) leave it ``None`` — back-compat with
    extractors and stored facts that predate the signature field.
    """

    kind: str
    source_symbol_id: str
    target_symbol_id: str
    file_path: str  # repo-relative, forward-slash normalized
    line: int  # 1-indexed
    language: str
    signature: Optional[Signature] = None

    def __post_init__(self) -> None:
        if self.kind not in FACT_KINDS:
            raise ValueError(
                f"Fact.kind must be one of {sorted(FACT_KINDS)}, got {self.kind!r}"
            )
        if self.line < 1:
            raise ValueError(
                f"Fact.line must be 1-indexed (>=1), got {self.line}"
            )
        if self.signature is not None and self.kind != "defines":
            raise ValueError(
                f"Fact.signature is only valid on kind='defines', got {self.kind!r}"
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

    Shape:

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


# ---------------------------------------------------------------------------
# Reference-resolution registry (ADR-055)
# ---------------------------------------------------------------------------


# A per-language *resolver* binds a file's cross-module references (calls /
# imports / extends) to the callee's DEFINITION symbol id, so
# ``symbol_graph_reader.callers``/``importers`` of a definition return the real
# cross-file consumers. It is the language-specific half of ADR-055; the
# language-agnostic pass lives in ``extractor.resolution``.
#
# Contract: given one file's facts and a repo definitions index, return a remap
# ``{raw_target_symbol_id: resolved_target_symbol_id}`` for that file's
# reference facts. Only entries that bind UNAMBIGUOUSLY to a known definition
# are returned; everything else is omitted and the framework leaves the raw
# target unchanged (ADR-055 never-guess). The index is typed ``object`` here to
# avoid a circular import with ``extractor.resolution``.
ResolverFn = Callable[[List["Fact"], object], Dict[str, str]]


_RESOLVER_REGISTRY: Dict[str, ResolverFn] = {}


def register_resolver(language: str, fn: ResolverFn) -> None:
    """Register a per-language reference resolver (ADR-055)."""

    _RESOLVER_REGISTRY[language] = fn


def get_resolver(language: str) -> Optional[ResolverFn]:
    """Return the registered resolver for ``language`` or None.

    A language with no resolver keeps raw (unresolved) reference targets — no
    regression, and languages are enabled incrementally per ADR-055.
    """

    return _RESOLVER_REGISTRY.get(language)


def resolver_languages() -> Set[str]:
    """Return the set of languages that currently have a resolver."""

    return set(_RESOLVER_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Per-repo extraction filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolGraphFilter:
    """Per-repo decisions about whether a given file participates in
    symbol-graph extraction.

    The dataclass mirrors the Pydantic ``SymbolGraphConfig`` shape from
    ``lambda/shared/repo_config.py`` but is intentionally a plain
    dataclass — it represents an INTERNAL pre-validated decision object
    threaded through the extraction call site, not user input. The
    Pydantic boundary lives at config parse time; once we hand a
    ``SymbolGraphFilter`` to the extractor we know the values are sound.

    Precedence (applied in ``should_extract``, top-most wins):

    1. ``enabled=False`` — short-circuits to False, no facts emitted.
    2. ADR-009 base path excludes (``EXCLUDED_PATH_FRAGMENTS``) — these
       always win regardless of repo-level overrides because the
       code-unit discoverer skips them too.
    3. Repo-level ``exclude`` globs — beat ``include``.
    4. Repo-level ``include`` globs — empty list = include everything
       that survives the excludes.
    5. ``languages`` allow-list — empty / unset = all registered.
    6. ``max_file_bytes`` cap — 0 means "no cap".
    """

    enabled: bool = True
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    max_file_bytes: int = 524288
    languages: Optional[List[str]] = None

    def should_extract(
        self, file_path: str, language: str, size_bytes: int
    ) -> bool:
        """Return True when ``file_path`` participates in extraction."""

        # (1) master switch
        if not self.enabled:
            return False

        # (2) ADR-009 base excludes — never negotiable
        if is_excluded_path(file_path):
            return False

        # (3) repo-level excludes
        normalized = file_path.replace("\\", "/")
        for pat in self.exclude:
            if fnmatch.fnmatch(normalized, pat):
                return False

        # (4) repo-level includes (empty list = include all)
        if self.include:
            included = any(
                fnmatch.fnmatch(normalized, pat) for pat in self.include
            )
            if not included:
                return False

        # (5) language allow-list
        if self.languages is not None and language not in self.languages:
            return False

        # (6) size cap (0 disables the cap)
        if self.max_file_bytes and size_bytes > self.max_file_bytes:
            return False

        return True


# Module-level default — permissive everything. Used when no repo config
# was loaded (the existing legacy call sites) and as the safe fallback
# from ``handler._load_symbol_graph_filter`` when config parsing fails.
DEFAULT_SYMBOL_GRAPH_FILTER = SymbolGraphFilter()
