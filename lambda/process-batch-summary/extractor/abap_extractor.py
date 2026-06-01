"""
ABAP symbol/edge extractor — hybrid Tree-sitter + regex (ADR-020).

The 2026-05-14 spike showed:

  * The FrankRogalski tree-sitter-abap grammar (commit ``7dcb098``)
    parses OO ABAP cleanly — class definitions, method bodies, calls.
  * The same grammar errors on procedural ABAP (``FUNCTION-POOL``,
    ``START-OF-SELECTION``, event blocks).

So this extractor sniffs the source header and dispatches:

  1. If the file contains ``CLASS ... DEFINITION`` or
     ``CLASS ... IMPLEMENTATION`` -> tree-sitter backend.
  2. If the file contains procedural markers (``FUNCTION-POOL``,
     ``START-OF-SELECTION``, ``FORM``, ``REPORT``) -> regex backend.
  3. Default -> regex backend (safe fallback).

The tree-sitter backend is imported lazily so the regex path (and the
test suite) work even when the FrankRogalski wheel isn't available in
the build environment. If the grammar import fails at runtime, this
module logs a warning and falls back to the regex backend.

Reference: docs/adr/ADR-020-abap-parser-strategy.md.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, List, Optional

from .base import Fact, Param, Signature, make_symbol_id, register_language


_ABAP_PARAM_HEADERS = ("USING", "CHANGING", "RETURNING", "EXPORTING", "IMPORTING")
# `pname TYPE typestr` or `VALUE(pname) TYPE typestr` or bare `pname`.
_ABAP_PARAM_RE = re.compile(
    r"(?:VALUE\s*\(\s*)?(?P<name>[A-Z_][A-Z0-9_]*)\s*\)?"
    r"(?:\s+TYPE\s+(?P<type>[A-Z_][A-Z0-9_=>:\->\s]*?))?(?=\s|$)",
    re.IGNORECASE,
)


def _extract_signature_abap_form(params_text: str) -> Signature:
    """Best-effort signature capture for an ABAP FORM declaration.

    The params text is everything between the FORM name and the
    terminating period. ABAP groups parameters under section headers
    (``USING`` / ``CHANGING`` / ``RETURNING`` / etc.); we surface the
    individual parameter names + types, ignoring the section grouping
    (consumers see the full callable shape without needing ABAP-specific
    handling). The section keywords themselves are not captured.

    Methods + Functions are intentionally NOT signature-captured here --
    their parameter declarations live in CLASS DEFINITION / function-pool
    blocks separate from the IMPLEMENTATION statement, which a regex
    extractor cannot reliably resolve. Follow-up if needed.
    """

    if not params_text:
        return Signature()

    # Strip section headers so the param regex doesn't match them.
    body = params_text
    for header in _ABAP_PARAM_HEADERS:
        body = re.sub(rf"\b{header}\b", " ", body, flags=re.IGNORECASE)

    params: List[Param] = []
    for m in _ABAP_PARAM_RE.finditer(body):
        name = m.group("name")
        if name.upper() in _ABAP_PARAM_HEADERS:
            continue
        type_str = m.group("type")
        type_str = type_str.strip() if type_str else None
        params.append(Param(name=name.lower(), type=type_str))
    return Signature(params=tuple(params))

logger = logging.getLogger(__name__)


_OO_HEADER_RE = re.compile(
    r"\bCLASS\s+[A-Z_/][A-Z0-9_/]*\s+(DEFINITION|IMPLEMENTATION)",
    re.IGNORECASE,
)
_PROCEDURAL_RE = re.compile(
    r"\b(FUNCTION-POOL|START-OF-SELECTION|REPORT\s+|FORM\s+)",
    re.IGNORECASE,
)

_CLASS_DEF_RE = re.compile(
    r"^\s*CLASS\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s+(DEFINITION|IMPLEMENTATION)",
    re.IGNORECASE | re.MULTILINE,
)
_METHOD_DEF_RE = re.compile(
    r"^\s*METHOD\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*(?:\.|\s)",
    re.IGNORECASE | re.MULTILINE,
)
_FORM_RE = re.compile(
    r"^\s*FORM\s+(?P<name>[A-Z_/][A-Z0-9_/]*)(?P<params>[^.]*?)\.",
    re.IGNORECASE | re.MULTILINE,
)
_FUNCTION_RE = re.compile(
    r"^\s*FUNCTION\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*(?:\.|\s)",
    re.IGNORECASE | re.MULTILINE,
)
_INCLUDE_RE = re.compile(
    r"^\s*INCLUDE\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*\.",
    re.IGNORECASE | re.MULTILINE,
)
_CALL_FORM_RE = re.compile(
    r"\bPERFORM\s+(?P<name>[A-Z_/][A-Z0-9_/]*)",
    re.IGNORECASE,
)
_CALL_METHOD_RE = re.compile(
    r"\bCALL\s+METHOD\s+(?P<name>[A-Z_/][A-Z0-9_/=>-]+)",
    re.IGNORECASE,
)
_CALL_FUNCTION_RE = re.compile(
    r"\bCALL\s+FUNCTION\s+'(?P<name>[A-Z_/][A-Z0-9_/]*)'",
    re.IGNORECASE,
)


def _module_path(file_path: str) -> str:
    cleaned = file_path.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _looks_oo(text: str) -> bool:
    return bool(_OO_HEADER_RE.search(text))


def _looks_procedural(text: str) -> bool:
    return bool(_PROCEDURAL_RE.search(text))


def _extract_via_regex(file_path: str, source: bytes) -> Iterator[Fact]:
    """Pure regex backend — procedural ABAP and the safe fallback."""

    text = source.decode("utf-8", errors="replace")
    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("abap", module, "<module>")

    # INCLUDE -> imports
    for m in _INCLUDE_RE.finditer(text):
        name = m.group("name").upper()
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", name, "*"),
            file_path=file_path,
            line=_line_for_offset(text, m.start()),
            language="abap",
        )

    # Class spans (for namespace assignment to METHODs)
    class_spans: List[tuple[int, int, str]] = []
    matches = list(_CLASS_DEF_RE.finditer(text))
    for i, cm in enumerate(matches):
        # End the span at the next class header (or EOF).
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        class_spans.append((cm.start(), end, cm.group("name").upper()))
        cls_name = cm.group("name").upper()
        cls_id = make_symbol_id("abap", module, cls_name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=cls_id,
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )

    seen_methods: set[str] = set()
    for mm in _METHOD_DEF_RE.finditer(text):
        method_name = mm.group("name").upper()
        # Find enclosing class
        enclosing: Optional[str] = None
        for span_start, span_end, cls in class_spans:
            if span_start <= mm.start() <= span_end:
                enclosing = cls
                break
        if enclosing is None:
            continue
        key = f"{enclosing}::{method_name}"
        if key in seen_methods:
            continue
        seen_methods.add(key)
        method_id = make_symbol_id("abap", module, method_name, namespace=enclosing)
        cls_id = make_symbol_id("abap", module, enclosing)
        yield Fact(
            kind="defines",
            source_symbol_id=cls_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=_line_for_offset(text, mm.start("name")),
            language="abap",
        )

    # FORM and FUNCTION definitions (procedural)
    for fm in _FORM_RE.finditer(text):
        name = fm.group("name").upper()
        form_id = make_symbol_id("abap", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=form_id,
            file_path=file_path,
            line=_line_for_offset(text, fm.start("name")),
            language="abap",
            signature=_extract_signature_abap_form(fm.group("params")),
        )

    for fnm in _FUNCTION_RE.finditer(text):
        name = fnm.group("name").upper()
        fn_id = make_symbol_id("abap", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=fn_id,
            file_path=file_path,
            line=_line_for_offset(text, fnm.start("name")),
            language="abap",
        )

    # Calls: PERFORM (FORM), CALL METHOD, CALL FUNCTION
    for cm in _CALL_FORM_RE.finditer(text):
        name = cm.group("name").upper()
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", module, name),
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )
    for cm in _CALL_METHOD_RE.finditer(text):
        raw = cm.group("name").upper()
        # Strip class qualifier (e.g. ``ZCL_X=>METHOD`` -> ``METHOD`` on ``ZCL_X``)
        if "=>" in raw:
            cls, sym = raw.split("=>", 1)
            target = make_symbol_id("abap", cls, sym)
        elif "->" in raw:
            _, sym = raw.split("->", 1)
            target = make_symbol_id("abap", module, sym)
        else:
            target = make_symbol_id("abap", module, raw)
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=target,
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )
    for cm in _CALL_FUNCTION_RE.finditer(text):
        name = cm.group("name").upper()
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", name, "*"),
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )


_TS_PARSER = None  # lazily constructed; module-level cache.


def _get_tree_sitter_parser():
    """Return a cached tree-sitter Parser bound to the ABAP grammar.

    Returns ``None`` when the FrankRogalski grammar wheel isn't
    available in the runtime — caller should fall back to regex.
    """

    global _TS_PARSER
    if _TS_PARSER is not None:
        return _TS_PARSER
    try:
        import tree_sitter_abap as tsa  # type: ignore
        from tree_sitter import Language, Parser  # type: ignore
    except Exception:
        return None
    try:
        _TS_PARSER = Parser(Language(tsa.language()))
        return _TS_PARSER
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tree-sitter-abap parser construction failed: %s", exc)
        return None


def _ts_node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _ts_first_child_named(node, name: str):
    """Return the first child whose ``type`` equals ``name``, else None."""

    for child in node.children:
        if child.type == name:
            return child
    return None


def _ts_children_named(node, name: str):
    return [c for c in node.children if c.type == name]


def _ts_class_name(node, source: bytes) -> Optional[str]:
    """Return the upper-cased class/method/function ``name`` child text."""

    n = _ts_first_child_named(node, "name")
    if n is None:
        return None
    return _ts_node_text(n, source).upper()


def _ts_collect_object_types(class_impl_node, source: bytes) -> dict:
    """Best-effort: map local var name -> referenced type within a class impl.

    Walks ``chained_variable_declaration`` and ``variable_declaration``
    descendants. Each ``variable`` child carries a ``name`` and an
    optional ``typing`` subtree whose final ``type`` token is the
    referenced class name (e.g. ``DATA: lo TYPE REF TO zcl_other.``
    yields ``{"LO": "ZCL_OTHER"}``).
    """

    result: dict = {}
    stack = list(class_impl_node.children)
    while stack:
        cur = stack.pop()
        if cur.type in ("chained_variable_declaration", "variable_declaration"):
            for var in cur.children:
                if var.type != "variable":
                    continue
                var_name = _ts_first_child_named(var, "name")
                if var_name is None:
                    continue
                typing = _ts_first_child_named(var, "typing")
                if typing is None:
                    continue
                # The last ``type`` child holds the referenced class name.
                type_children = _ts_children_named(typing, "type")
                if not type_children:
                    continue
                result[_ts_node_text(var_name, source).upper()] = _ts_node_text(
                    type_children[-1], source
                ).upper()
        else:
            stack.extend(cur.children)
    return result


def _ts_walk_method_body(
    body_node,
    source: bytes,
    file_path: str,
    method_source_id: str,
    module: str,
    object_types: dict,
) -> Iterator[Fact]:
    """Walk a method/function body and emit ``calls``/``imports`` facts.

    Recognises:
      * ``call_method_instance``   -> ``obj->m( )``
      * ``call_method_static``     -> ``Cls=>m( )``
      * ``call_function``          -> ``CALL FUNCTION 'NAME'.``
      * ``create_object_statement``-> ``CREATE OBJECT lo.`` (resolves to
                                       the declared class when known)
      * ``perform_statement``      -> ``PERFORM helper.``
      * ``include_statement``      -> ``INCLUDE z_top.``  (yields ``imports``)

    Also picks up the legacy ``CALL METHOD Cls=>method.`` form, which the
    grammar surfaces as a ``macro_include`` node named ``METHOD`` with
    an ``attribute_access_static`` parameter — we treat it as a static
    method call rather than a macro invocation.
    """

    stack = list(body_node.children)
    while stack:
        cur = stack.pop()
        kind = cur.type

        if kind == "call_method_instance":
            names = _ts_children_named(cur, "name")
            if len(names) >= 2:
                obj = _ts_node_text(names[0], source).upper()
                method = _ts_node_text(names[1], source).upper()
                # Try to resolve obj -> declared class; fall back to module.
                cls = object_types.get(obj, module)
                yield Fact(
                    kind="calls",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id("abap", cls, method),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            # No need to recurse into children of a call — args rarely add facts.
            continue

        if kind == "call_method_static":
            names = _ts_children_named(cur, "name")
            if len(names) >= 2:
                cls = _ts_node_text(names[0], source).upper()
                method = _ts_node_text(names[1], source).upper()
                yield Fact(
                    kind="calls",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id("abap", cls, method),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            continue

        if kind == "call_function":
            lit = _ts_first_child_named(cur, "character_literal")
            if lit is not None:
                # Strip surrounding quotes.
                name = _ts_node_text(lit, source).strip("'\"").upper()
                yield Fact(
                    kind="calls",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id("abap", name, "*"),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            continue

        if kind == "create_object_statement":
            n = _ts_first_child_named(cur, "name")
            if n is not None:
                var = _ts_node_text(n, source).upper()
                target_cls = object_types.get(var, var)
                yield Fact(
                    kind="calls",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id(
                        "abap", target_cls, "<constructor>"
                    ),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            continue

        if kind == "perform_statement":
            n = _ts_first_child_named(cur, "name")
            if n is not None:
                target = _ts_node_text(n, source).upper()
                yield Fact(
                    kind="calls",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id("abap", module, target),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            continue

        if kind == "include_statement":
            n = _ts_first_child_named(cur, "name")
            if n is not None:
                target = _ts_node_text(n, source).upper()
                yield Fact(
                    kind="imports",
                    source_symbol_id=method_source_id,
                    target_symbol_id=make_symbol_id("abap", target, "*"),
                    file_path=file_path,
                    line=cur.start_point[0] + 1,
                    language="abap",
                )
            continue

        if kind == "macro_include":
            # The grammar misparses ``CALL METHOD Cls=>method.`` as a
            # ``macro_include`` named ``METHOD`` whose only parameter is
            # an ``attribute_access_static``. Pattern-match that shape so
            # the legacy static-call syntax still emits a ``calls`` fact.
            mname = _ts_first_child_named(cur, "name")
            params = _ts_first_child_named(cur, "parameter_list")
            if (
                mname is not None
                and params is not None
                and _ts_node_text(mname, source).upper() == "METHOD"
            ):
                attr = _ts_first_child_named(params, "attribute_access_static")
                if attr is not None:
                    names = _ts_children_named(attr, "name")
                    if len(names) >= 2:
                        cls = _ts_node_text(names[0], source).upper()
                        method = _ts_node_text(names[1], source).upper()
                        yield Fact(
                            kind="calls",
                            source_symbol_id=method_source_id,
                            target_symbol_id=make_symbol_id("abap", cls, method),
                            file_path=file_path,
                            line=cur.start_point[0] + 1,
                            language="abap",
                        )
                        continue

        # Default: descend.
        stack.extend(cur.children)


def _extract_via_tree_sitter(file_path: str, source: bytes) -> Iterator[Fact]:
    """Tree-sitter backend for OO ABAP (ADR-020 v1.1 walk).

    Walks the FrankRogalski grammar parse tree for the OO node kinds
    documented in ADR-020:

      * ``class_declaration`` / ``class_implementation``
      * ``method_declaration`` / ``method_implementation``
      * ``function_implementation``
      * ``call_method_instance`` / ``call_method_static`` / ``call_function``
      * ``create_object_statement``
      * ``perform_statement``
      * ``include_statement`` / ``report_statement``

    Procedural-only constructs (``START-OF-SELECTION``, ``FUNCTION-POOL``,
    event blocks, ``FORM``) are not in this grammar's vocabulary and are
    handled by the regex backend — the dispatcher in ``extract()`` routes
    them there.

    Falls back to the regex backend when the grammar wheel isn't
    available in the runtime environment (ADR-020 dormancy contingency).
    """

    parser = _get_tree_sitter_parser()
    if parser is None:
        logger.info(
            "tree-sitter-abap grammar not available; falling back to regex for %s",
            file_path,
        )
        yield from _extract_via_regex(file_path, source)
        return

    try:
        tree = parser.parse(source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "tree-sitter-abap parse failed for %s: %s; falling back to regex",
            file_path,
            exc,
        )
        yield from _extract_via_regex(file_path, source)
        return

    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("abap", module, "<module>")
    root = tree.root_node

    for child in root.children:
        # Top-level INCLUDE / REPORT live alongside class bodies in
        # ABAP — emit them against the module before we descend into
        # OO scopes.
        if child.type == "include_statement":
            n = _ts_first_child_named(child, "name")
            if n is not None:
                target = _ts_node_text(n, source).upper()
                yield Fact(
                    kind="imports",
                    source_symbol_id=module_symbol_id,
                    target_symbol_id=make_symbol_id("abap", target, "*"),
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                    language="abap",
                )
            continue

        if child.type == "report_statement":
            n = _ts_first_child_named(child, "name")
            if n is not None:
                report_name = _ts_node_text(n, source).upper()
                yield Fact(
                    kind="defines",
                    source_symbol_id=module_symbol_id,
                    target_symbol_id=make_symbol_id("abap", module, report_name),
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                    language="abap",
                )
            continue

        if child.type == "function_implementation":
            fn_name = _ts_class_name(child, source)
            if fn_name is None:
                continue
            fn_id = make_symbol_id("abap", module, fn_name)
            yield Fact(
                kind="defines",
                source_symbol_id=module_symbol_id,
                target_symbol_id=fn_id,
                file_path=file_path,
                line=child.start_point[0] + 1,
                language="abap",
            )
            # Function bodies may contain calls — walk them.
            for c in child.children:
                if c.type == "method_body":
                    yield from _ts_walk_method_body(
                        c, source, file_path, fn_id, module, {}
                    )
            continue

        if child.type == "class_declaration":
            cls_name = _ts_class_name(child, source)
            if cls_name is None:
                continue
            cls_id = make_symbol_id("abap", module, cls_name)
            yield Fact(
                kind="defines",
                source_symbol_id=module_symbol_id,
                target_symbol_id=cls_id,
                file_path=file_path,
                line=child.start_point[0] + 1,
                language="abap",
            )
            # Walk method declarations inside each section.
            for section in child.children:
                if section.type not in (
                    "public_section",
                    "private_section",
                    "protected_section",
                ):
                    continue
                for member in section.children:
                    if member.type != "method_declaration":
                        continue
                    m_name = _ts_class_name(member, source)
                    if m_name is None:
                        continue
                    method_id = make_symbol_id(
                        "abap", module, m_name, namespace=cls_name
                    )
                    yield Fact(
                        kind="defines",
                        source_symbol_id=cls_id,
                        target_symbol_id=method_id,
                        file_path=file_path,
                        line=member.start_point[0] + 1,
                        language="abap",
                    )
            continue

        if child.type == "class_implementation":
            cls_name = _ts_class_name(child, source)
            if cls_name is None:
                continue
            cls_id = make_symbol_id("abap", module, cls_name)
            # Class-impl is *also* a ``defines`` site — having both
            # ``class_declaration`` and ``class_implementation`` in one
            # file is the canonical OO ABAP shape, so we emit twice on
            # purpose: the storage layer (chunk 02) dedupes on
            # (kind, source, target).
            yield Fact(
                kind="defines",
                source_symbol_id=module_symbol_id,
                target_symbol_id=cls_id,
                file_path=file_path,
                line=child.start_point[0] + 1,
                language="abap",
            )
            object_types = _ts_collect_object_types(child, source)
            for member in child.children:
                if member.type != "method_implementation":
                    continue
                m_name = _ts_class_name(member, source)
                if m_name is None:
                    continue
                method_id = make_symbol_id(
                    "abap", module, m_name, namespace=cls_name
                )
                yield Fact(
                    kind="defines",
                    source_symbol_id=cls_id,
                    target_symbol_id=method_id,
                    file_path=file_path,
                    line=member.start_point[0] + 1,
                    language="abap",
                )
                body = _ts_first_child_named(member, "method_body")
                if body is None:
                    continue
                yield from _ts_walk_method_body(
                    body, source, file_path, method_id, module, object_types
                )
            continue


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Hybrid dispatcher — header-sniff for OO vs procedural."""

    text = source.decode("utf-8", errors="replace")
    if _looks_oo(text):
        yield from _extract_via_tree_sitter(file_path, source)
    else:
        yield from _extract_via_regex(file_path, source)


register_language("abap", extract)
