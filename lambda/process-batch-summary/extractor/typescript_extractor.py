"""
TypeScript/TSX symbol/edge extractor backed by Tree-sitter.

Emits:

  * ``defines`` for every top-level function, class, and arrow-function
    assigned to a top-level ``const``/``let``. Class methods are emitted
    with the class as their namespace.
  * ``imports`` for every ``import`` form including ``import {a,b}``,
    ``import * as foo``, ``import defaultExport``, and side-effect
    imports (``import 'x'`` -> ``*``).
  * ``calls`` for every call expression in a function or method body.
  * ``extends`` for ``class X extends Y``.
  * ``implements`` for ``class X implements Y, Z``.

Path-alias resolution (the plan's open question on ``@/lib/api`` via
``tsconfig.json``) is deferred — for v1 we emit the literal import
specifier and let the query layer normalize aliases. Subpath imports
(``@hyatt-hpe/hpe-utils/http2``) flow through unchanged so the prefix-
match query in chunk 04 works.
"""

from __future__ import annotations

import logging
from typing import Iterator, List, Optional

import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from .base import Fact, make_symbol_id, register_language

logger = logging.getLogger(__name__)

_LANGUAGE_TS = Language(tsts.language_typescript())
_LANGUAGE_TSX = Language(tsts.language_tsx())
_PARSER_TS = Parser(_LANGUAGE_TS)
_PARSER_TSX = Parser(_LANGUAGE_TSX)


def _module_path(file_path: str) -> str:
    cleaned = file_path.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    for suffix in (".tsx", ".ts", ".jsx", ".js"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: Node, field: str) -> Optional[Node]:
    return node.child_by_field_name(field)


def _string_literal_value(node: Node, source: bytes) -> Optional[str]:
    if node.type != "string":
        return None
    for child in node.children:
        if child.type == "string_fragment":
            return _node_text(child, source)
    return None


def _emit_imports(
    node: Node, source: bytes, file_path: str, module_symbol_id: str
) -> Iterator[Fact]:
    line = node.start_point[0] + 1
    source_str: Optional[str] = None
    for child in node.children:
        if child.type == "string":
            source_str = _string_literal_value(child, source)
            break
    if not source_str:
        return

    clause: Optional[Node] = None
    for child in node.children:
        if child.type == "import_clause":
            clause = child
            break

    names: List[str] = []
    if clause is None:
        # Side-effect import: ``import 'x'``
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("typescript", source_str, "*"),
            file_path=file_path,
            line=line,
            language="typescript",
        )
        return

    for child in clause.children:
        if child.type == "identifier":
            names.append(_node_text(child, source))
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type == "import_specifier":
                    name_node = _child_by_field(spec, "name") or next(
                        (c for c in spec.children if c.type == "identifier"),
                        None,
                    )
                    if name_node is not None:
                        names.append(_node_text(name_node, source))
        elif child.type == "namespace_import":
            names.append("*")

    if not names:
        names.append("*")

    for name in names:
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("typescript", source_str, name),
            file_path=file_path,
            line=line,
            language="typescript",
        )


def _walk_calls(node: Node, source: bytes) -> Iterator[tuple[str, int]]:
    stack: List[Node] = list(node.children)
    while stack:
        current = stack.pop()
        if current.type == "call_expression":
            fn = _child_by_field(current, "function")
            if fn is not None:
                name = _callee_name(fn, source)
                if name:
                    yield name, current.start_point[0] + 1
        stack.extend(current.children)


def _callee_name(fn_node: Node, source: bytes) -> Optional[str]:
    if fn_node.type == "identifier":
        return _node_text(fn_node, source)
    if fn_node.type == "member_expression":
        prop = _child_by_field(fn_node, "property")
        obj = _child_by_field(fn_node, "object")
        if prop is None:
            return None
        prop_name = _node_text(prop, source)
        if obj is not None and obj.type == "this":
            return prop_name
        if obj is not None and obj.type == "identifier":
            return f"{_node_text(obj, source)}.{prop_name}"
        return prop_name
    return None


def _emit_class(
    class_node: Node,
    source: bytes,
    file_path: str,
    module: str,
    module_symbol_id: str,
) -> Iterator[Fact]:
    name_node = _child_by_field(class_node, "name")
    if name_node is None:
        return
    class_name = _node_text(name_node, source)
    class_id = make_symbol_id("typescript", module, class_name)
    yield Fact(
        kind="defines",
        source_symbol_id=module_symbol_id,
        target_symbol_id=class_id,
        file_path=file_path,
        line=class_node.start_point[0] + 1,
        language="typescript",
    )

    for child in class_node.children:
        if child.type == "class_heritage":
            for clause in child.children:
                if clause.type == "extends_clause":
                    for n in clause.children:
                        if n.type in ("identifier", "type_identifier"):
                            yield Fact(
                                kind="extends",
                                source_symbol_id=class_id,
                                target_symbol_id=make_symbol_id(
                                    "typescript", _node_text(n, source), "*"
                                ),
                                file_path=file_path,
                                line=n.start_point[0] + 1,
                                language="typescript",
                            )
                elif clause.type == "implements_clause":
                    for n in clause.children:
                        if n.type in ("type_identifier", "identifier"):
                            yield Fact(
                                kind="implements",
                                source_symbol_id=class_id,
                                target_symbol_id=make_symbol_id(
                                    "typescript", _node_text(n, source), "*"
                                ),
                                file_path=file_path,
                                line=n.start_point[0] + 1,
                                language="typescript",
                            )

    body = _child_by_field(class_node, "body")
    if body is None:
        return
    for member in body.children:
        if member.type != "method_definition":
            continue
        m_name_node = _child_by_field(member, "name")
        if m_name_node is None:
            continue
        m_name = _node_text(m_name_node, source)
        method_id = make_symbol_id("typescript", module, m_name, namespace=class_name)
        yield Fact(
            kind="defines",
            source_symbol_id=class_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=member.start_point[0] + 1,
            language="typescript",
        )
        m_body = _child_by_field(member, "body")
        if m_body is None:
            continue
        for callee, line in _walk_calls(m_body, source):
            yield Fact(
                kind="calls",
                source_symbol_id=method_id,
                target_symbol_id=make_symbol_id("typescript", module, callee),
                file_path=file_path,
                line=line,
                language="typescript",
            )


def _emit_function(
    fn_node: Node,
    source: bytes,
    file_path: str,
    module: str,
    module_symbol_id: str,
) -> Iterator[Fact]:
    name_node = _child_by_field(fn_node, "name")
    if name_node is None:
        return
    name = _node_text(name_node, source)
    fn_id = make_symbol_id("typescript", module, name)
    yield Fact(
        kind="defines",
        source_symbol_id=module_symbol_id,
        target_symbol_id=fn_id,
        file_path=file_path,
        line=fn_node.start_point[0] + 1,
        language="typescript",
    )
    body = _child_by_field(fn_node, "body")
    if body is None:
        return
    for callee, line in _walk_calls(body, source):
        yield Fact(
            kind="calls",
            source_symbol_id=fn_id,
            target_symbol_id=make_symbol_id("typescript", module, callee),
            file_path=file_path,
            line=line,
            language="typescript",
        )


def _emit_const_arrow(
    decl_node: Node,
    source: bytes,
    file_path: str,
    module: str,
    module_symbol_id: str,
) -> Iterator[Fact]:
    """Handle ``const foo = (x) => {...}`` / ``const foo = function() {}``."""

    for child in decl_node.children:
        if child.type != "variable_declarator":
            continue
        name_node = _child_by_field(child, "name")
        value_node = _child_by_field(child, "value")
        if name_node is None or value_node is None:
            continue
        if value_node.type not in ("arrow_function", "function_expression"):
            continue
        if name_node.type != "identifier":
            continue
        name = _node_text(name_node, source)
        fn_id = make_symbol_id("typescript", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=fn_id,
            file_path=file_path,
            line=child.start_point[0] + 1,
            language="typescript",
        )
        body = _child_by_field(value_node, "body")
        if body is None:
            continue
        for callee, line in _walk_calls(body, source):
            yield Fact(
                kind="calls",
                source_symbol_id=fn_id,
                target_symbol_id=make_symbol_id("typescript", module, callee),
                file_path=file_path,
                line=line,
                language="typescript",
            )


def _iter_top_level(root: Node) -> Iterator[Node]:
    """Yield each top-level statement, unwrapping ``export`` wrappers."""

    for child in root.children:
        if child.type in ("export_statement",):
            inner_decl = _child_by_field(child, "declaration")
            if inner_decl is not None:
                yield inner_decl
            else:
                yield child
        else:
            yield child


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Yield Facts for a TS or TSX source file."""

    parser = _PARSER_TSX if file_path.endswith(".tsx") else _PARSER_TS
    try:
        tree = parser.parse(source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("typescript tree-sitter parse failed for %s: %s", file_path, exc)
        return

    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("typescript", module, "<module>")

    for node in _iter_top_level(tree.root_node):
        t = node.type
        if t == "import_statement":
            yield from _emit_imports(node, source, file_path, module_symbol_id)
        elif t == "class_declaration":
            yield from _emit_class(node, source, file_path, module, module_symbol_id)
        elif t == "function_declaration":
            yield from _emit_function(
                node, source, file_path, module, module_symbol_id
            )
        elif t in ("lexical_declaration", "variable_declaration"):
            yield from _emit_const_arrow(
                node, source, file_path, module, module_symbol_id
            )


register_language("typescript", extract)
