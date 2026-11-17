# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Java symbol/edge extractor backed by Tree-sitter.

Emits:

  * ``defines`` for each top-level class/interface/enum/record, plus
    methods declared in those types.
  * ``imports`` for every ``import`` (one fact per imported FQN; a
    wildcard ``import a.b.*`` yields a ``*`` target).
  * ``calls`` for every method invocation in a method body.
  * ``extends`` for ``class X extends Y``.
  * ``implements`` for ``class X implements I1, I2``.

Module path is the Java package (e.g. ``com.acme.booking``). Symbol
namespace is the enclosing class name. Inner classes are flattened to
one level — explicitly skips deep nested structure to keep
the edge volume manageable.
"""

from __future__ import annotations

import logging
from typing import Iterator, List, Optional

import tree_sitter_java as tsj
from tree_sitter import Language, Node, Parser

from .base import Fact, Param, Signature, make_symbol_id, register_language

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tsj.language())
_PARSER = Parser(_LANGUAGE)


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: Node, field: str) -> Optional[Node]:
    return node.child_by_field_name(field)


def _find_package(root: Node, source: bytes) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            for n in child.children:
                if n.type in ("scoped_identifier", "identifier"):
                    return _node_text(n, source)
    return ""


def _emit_imports(
    node: Node, source: bytes, file_path: str, module_symbol_id: str
) -> Iterator[Fact]:
    line = node.start_point[0] + 1
    is_wildcard = any(c.type == "asterisk" for c in node.children)
    name_node: Optional[Node] = None
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            name_node = child
    if name_node is None:
        return
    full = _node_text(name_node, source)
    if is_wildcard:
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("java", full, "*"),
            file_path=file_path,
            line=line,
            language="java",
        )
        return
    # Split package vs symbol on last dot.
    if "." in full:
        pkg, sym = full.rsplit(".", 1)
    else:
        pkg, sym = full, "*"
    yield Fact(
        kind="imports",
        source_symbol_id=module_symbol_id,
        target_symbol_id=make_symbol_id("java", pkg, sym),
        file_path=file_path,
        line=line,
        language="java",
    )


def _extract_signature_java(method_node: Node, source: bytes) -> Signature:
    """Build a Signature from a tree-sitter Java method/constructor node.

    Captures parameter names + declared types and the return type (for
    method_declaration; constructors return None). Defaults are not a
    Java concept at the parameter level, so no default-handling needed.
    """

    params: List[Param] = []
    formal_params = _child_by_field(method_node, "parameters")
    if formal_params is not None:
        for child in formal_params.children:
            if child.type == "formal_parameter":
                name_node = _child_by_field(child, "name")
                type_node = _child_by_field(child, "type")
                if name_node is None:
                    continue
                params.append(
                    Param(
                        name=_node_text(name_node, source),
                        type=_node_text(type_node, source) if type_node else None,
                    )
                )
            elif child.type == "spread_parameter":
                # spread_parameter children typically: type node + `...` +
                # variable_declarator (whose name field is the param name).
                # Field-based access is unreliable here across tree-sitter
                # versions, so we walk children: first identifier-shaped
                # child of a variable_declarator (or the spread_parameter
                # itself) is the name; the first type-shaped child is the
                # element type.
                name = None
                type_text = None
                for c in child.children:
                    if c.type == "variable_declarator":
                        nn = _child_by_field(c, "name")
                        if nn is not None:
                            name = _node_text(nn, source)
                        else:
                            for cc in c.children:
                                if cc.type == "identifier":
                                    name = _node_text(cc, source)
                                    break
                    elif name is None and c.type == "identifier":
                        name = _node_text(c, source)
                    elif type_text is None and c.type.endswith("_type"):
                        type_text = _node_text(c, source)
                if name:
                    params.append(
                        Param(
                            name="..." + name,
                            type=type_text,
                        )
                    )

    return_type_node = _child_by_field(method_node, "type")
    returns = _node_text(return_type_node, source) if return_type_node else None
    return Signature(params=tuple(params), returns=returns)


def _walk_calls(node: Node, source: bytes) -> Iterator[tuple[str, int]]:
    stack: List[Node] = list(node.children)
    while stack:
        current = stack.pop()
        if current.type == "method_invocation":
            name_node = _child_by_field(current, "name")
            if name_node is not None:
                yield _node_text(name_node, source), current.start_point[0] + 1
        stack.extend(current.children)


def _emit_class_like(
    class_node: Node,
    source: bytes,
    file_path: str,
    package: str,
    module_symbol_id: str,
) -> Iterator[Fact]:
    name_node = _child_by_field(class_node, "name")
    if name_node is None:
        return
    class_name = _node_text(name_node, source)
    class_id = make_symbol_id("java", package, class_name)
    yield Fact(
        kind="defines",
        source_symbol_id=module_symbol_id,
        target_symbol_id=class_id,
        file_path=file_path,
        line=class_node.start_point[0] + 1,
        language="java",
    )

    for child in class_node.children:
        if child.type == "superclass":
            for n in child.children:
                if n.type == "type_identifier":
                    yield Fact(
                        kind="extends",
                        source_symbol_id=class_id,
                        target_symbol_id=make_symbol_id(
                            "java", _node_text(n, source), "*"
                        ),
                        file_path=file_path,
                        line=n.start_point[0] + 1,
                        language="java",
                    )
        elif child.type in ("super_interfaces", "extends_interfaces"):
            for type_list in child.children:
                if type_list.type == "type_list":
                    for n in type_list.children:
                        if n.type == "type_identifier":
                            kind = (
                                "extends"
                                if child.type == "extends_interfaces"
                                else "implements"
                            )
                            yield Fact(
                                kind=kind,
                                source_symbol_id=class_id,
                                target_symbol_id=make_symbol_id(
                                    "java", _node_text(n, source), "*"
                                ),
                                file_path=file_path,
                                line=n.start_point[0] + 1,
                                language="java",
                            )

    body = _child_by_field(class_node, "body")
    if body is None:
        return
    for member in body.children:
        if member.type not in ("method_declaration", "constructor_declaration"):
            continue
        m_name_node = _child_by_field(member, "name")
        if m_name_node is None:
            continue
        m_name = _node_text(m_name_node, source)
        method_id = make_symbol_id("java", package, m_name, namespace=class_name)
        yield Fact(
            kind="defines",
            source_symbol_id=class_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=member.start_point[0] + 1,
            language="java",
            signature=_extract_signature_java(member, source),
        )
        m_body = _child_by_field(member, "body")
        if m_body is None:
            continue
        for callee, line in _walk_calls(m_body, source):
            yield Fact(
                kind="calls",
                source_symbol_id=method_id,
                target_symbol_id=make_symbol_id("java", package, callee),
                file_path=file_path,
                line=line,
                language="java",
            )


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Yield Facts for a Java source file."""

    try:
        tree = _PARSER.parse(source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("java tree-sitter parse failed for %s: %s", file_path, exc)
        return

    root = tree.root_node
    package = _find_package(root, source)
    module = package if package else file_path.rsplit("/", 1)[0]
    module_symbol_id = make_symbol_id("java", module, "<module>")

    for child in root.children:
        if child.type == "import_declaration":
            yield from _emit_imports(child, source, file_path, module_symbol_id)
        elif child.type in (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        ):
            yield from _emit_class_like(
                child, source, file_path, module, module_symbol_id
            )


register_language("java", extract)
