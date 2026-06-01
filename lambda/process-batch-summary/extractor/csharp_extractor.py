"""
C# symbol/edge extractor — regex-only per ADR-017.

ADR-017 binds every code-map backend to a regex/filesystem strategy for
C# (no tree-sitter, no Roslyn). This extractor follows the same rule so
the symbol-graph and the code-map stay on one mental model for C#.

Emits:

  * ``defines`` for top-level ``class`` / ``interface`` / ``record`` /
    ``struct``, and for methods inside those.
  * ``imports`` for every ``using X.Y;`` (one fact per directive).
  * ``calls`` for invocations syntactically resembling ``Name(`` or
    ``this.Name(`` within a method body. Property access without a call
    is intentionally not emitted.
  * ``extends`` and ``implements`` are both emitted from the ``: A, B``
    base-list. v1 cannot distinguish class-vs-interface targets without
    a project-wide symbol table, so the extractor emits both ``extends``
    and ``implements`` only when the textual context is unambiguous; in
    the ambiguous default case it emits ``extends`` for the first base
    (which the C# language requires to be the class if present) and
    ``implements`` for the rest. This is the documented v1 trade-off
    (see ADR-017 and ADR-020).

Known false-positive surfaces (per ADR-017's "tradeoffs" section):

  * Multi-line generics may swallow a base-list segment.
  * Methods inside expression-bodied members may be missed.
  * Raw string literals containing ``class`` or ``using`` are scoped
    out by requiring the keyword to start a line (after optional
    whitespace + modifiers).

These match the precedent the C# code-map backend already lives with.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional

from .base import Fact, Param, Signature, make_symbol_id, register_language


def _csharp_split_top_level(params_text: str) -> List[str]:
    """Split a C# parameter list on top-level commas, ignoring those
    inside generic angle brackets / arrays. Best-effort heuristic to
    support signature capture without a proper parser (ADR-017)."""

    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in params_text:
        if ch in "<[(":
            depth += 1
        elif ch in ">])":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


_CS_PARAM_MODS = {"ref", "out", "in", "params", "this"}


def _csharp_param_from_piece(piece: str) -> Optional[Param]:
    """Convert one C# parameter declaration like ``int x``, ``out string y``,
    or ``params object[] args`` into a ``Param``. The name is the last
    identifier-shaped token; everything before it (after stripping
    modifiers and a default-value suffix) is the declared type. Default
    values are dropped per ADR-027 -- literals stay out of the graph."""

    head = piece.split("=", 1)[0].strip()
    tokens = head.split()
    while tokens and tokens[0] in _CS_PARAM_MODS:
        tokens.pop(0)
    if not tokens:
        return None
    if len(tokens) == 1:
        return Param(name=tokens[0])
    name = tokens[-1]
    type_str = " ".join(tokens[:-1])
    return Param(name=name, type=type_str or None)


def _extract_signature_csharp(params_text: str, return_type: Optional[str]) -> Signature:
    """Build a best-effort Signature from C# regex captures. Names + types
    are extracted token-by-token; return type comes verbatim from the
    method regex's `return_type` group (stripped). Constructors leave
    return_type as None."""

    params: List[Param] = []
    for piece in _csharp_split_top_level(params_text):
        p = _csharp_param_from_piece(piece)
        if p is not None:
            params.append(p)
    rt = (return_type or "").strip() or None
    return Signature(params=tuple(params), returns=rt)


_NAMESPACE_RE = re.compile(
    r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
)
_USING_RE = re.compile(
    r"^\s*using\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*;",
    re.MULTILINE,
)
_TYPE_DECL_RE = re.compile(
    r"^\s*(?:public|internal|protected|private|static|sealed|abstract|partial|\s)*"
    r"(?P<kind>class|interface|record|struct)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*<[^>]*>)?"
    r"(?:\s*:\s*(?P<bases>[^{\r\n]+))?",
    re.MULTILINE,
)
_METHOD_RE = re.compile(
    r"^\s*(?:public|internal|protected|private|static|virtual|override|"
    r"abstract|sealed|async|new|extern|\s)*"
    r"(?P<return_type>[A-Za-z_][A-Za-z0-9_<>?,\[\]\s\.]*?\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)\)\s*(?:where[^{;]+)?\s*[{=]",
    re.MULTILINE,
)
_CALL_RE = re.compile(
    r"(?:^|[\s\(\[\{,;.+\-*/&|<>=!?:])"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("
)

_METHOD_KEYWORDS = {
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "catch",
    "using",
    "lock",
    "return",
    "yield",
    "throw",
    "await",
    "fixed",
    "new",
    "typeof",
    "sizeof",
    "default",
    "nameof",
    "checked",
    "unchecked",
    "stackalloc",
    "case",
}


def _file_namespace(source: str) -> str:
    m = _NAMESPACE_RE.search(source)
    return m.group(1) if m else ""


def _split_bases(bases: str) -> List[str]:
    """Best-effort split of a C# base-list.

    Strips generic-parameter brackets so we don't split inside ``Foo<A,B>``.
    """

    depth = 0
    parts: List[str] = []
    current = []
    for ch in bases:
        if ch == "<":
            depth += 1
            current.append(ch)
        elif ch == ">":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            token = "".join(current).strip()
            if token:
                parts.append(token)
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [_strip_generic(p) for p in parts if p]


def _strip_generic(s: str) -> str:
    """Remove ``<...>`` suffix so the canonical ID is bracket-free."""

    idx = s.find("<")
    return s[:idx].strip() if idx >= 0 else s.strip()


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _find_method_body(source: str, start: int) -> Optional[tuple[int, int]]:
    """Return (body_start, body_end) for a method whose ``{`` is at/after ``start``."""

    # Find the opening brace
    brace_open = source.find("{", start)
    if brace_open == -1:
        return None
    depth = 1
    i = brace_open + 1
    while i < len(source) and depth > 0:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return brace_open + 1, i
        i += 1
    return None


def _walk_calls_in_range(
    source: str, start: int, end: int
) -> Iterator[tuple[str, int]]:
    for m in _CALL_RE.finditer(source, start, end):
        name = m.group("name")
        if name in _METHOD_KEYWORDS:
            continue
        yield name, _line_for_offset(source, m.start("name"))


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Yield Facts for a C# source file."""

    text = source.decode("utf-8", errors="replace")
    namespace = _file_namespace(text)
    module = namespace if namespace else file_path.rsplit("/", 1)[0]
    module_symbol_id = make_symbol_id("csharp", module, "<module>")

    for m in _USING_RE.finditer(text):
        full = m.group(1)
        if "." in full:
            pkg, sym = full.rsplit(".", 1)
        else:
            pkg, sym = full, "*"
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("csharp", pkg, sym),
            file_path=file_path,
            line=_line_for_offset(text, m.start()),
            language="csharp",
        )

    type_matches = list(_TYPE_DECL_RE.finditer(text))
    # Pre-compute type-name spans for method-scope detection.
    type_spans: List[tuple[int, int, str]] = []
    for tm in type_matches:
        body_range = _find_method_body(text, tm.end())
        if body_range is not None:
            type_spans.append((body_range[0], body_range[1], tm.group("name")))

    for tm in type_matches:
        name = tm.group("name")
        type_kind = tm.group("kind")
        line = _line_for_offset(text, tm.start("name"))
        type_id = make_symbol_id("csharp", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=type_id,
            file_path=file_path,
            line=line,
            language="csharp",
        )
        bases_raw = tm.group("bases")
        if bases_raw:
            bases = _split_bases(bases_raw)
            for idx, base in enumerate(bases):
                # First base on a class is conventionally the class to extend
                # (when one exists). Interfaces emit ``implements``.
                base_kind = "extends" if (type_kind == "class" and idx == 0) else "implements"
                if type_kind in ("interface", "struct", "record"):
                    base_kind = "implements"
                yield Fact(
                    kind=base_kind,
                    source_symbol_id=type_id,
                    target_symbol_id=make_symbol_id("csharp", base, "*"),
                    file_path=file_path,
                    line=line,
                    language="csharp",
                )

    # Methods + calls. We scope each method match to the enclosing type span.
    for mm in _METHOD_RE.finditer(text):
        method_name = mm.group("name")
        if method_name in _METHOD_KEYWORDS:
            continue
        # Skip type declarations matched accidentally by the method regex
        if any(
            mm.start() == tm.start()
            for tm in type_matches
            if tm.start("name") == mm.start("name")
        ):
            continue
        # Find enclosing type span (innermost). If none, treat as module-scope.
        enclosing_type: Optional[str] = None
        for span_start, span_end, type_name in type_spans:
            if span_start <= mm.start() <= span_end:
                enclosing_type = type_name
                break
        if enclosing_type is None:
            continue  # Skip module-level methods (top-level statements rare in real code)

        method_id = make_symbol_id(
            "csharp", module, method_name, namespace=enclosing_type
        )
        type_id = make_symbol_id("csharp", module, enclosing_type)
        yield Fact(
            kind="defines",
            source_symbol_id=type_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=_line_for_offset(text, mm.start("name")),
            language="csharp",
            signature=_extract_signature_csharp(
                mm.group("params"), mm.group("return_type")
            ),
        )
        body_range = _find_method_body(text, mm.end() - 1)
        if body_range is None:
            continue
        for callee, line in _walk_calls_in_range(text, body_range[0], body_range[1]):
            yield Fact(
                kind="calls",
                source_symbol_id=method_id,
                target_symbol_id=make_symbol_id("csharp", module, callee),
                file_path=file_path,
                line=line,
                language="csharp",
            )


register_language("csharp", extract)
