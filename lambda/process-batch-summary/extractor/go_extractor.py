"""
Go symbol/edge extractor backed by Tree-sitter.

Emits:

  * ``defines`` for every top-level ``func`` (including methods — i.e.
    ``func (r Receiver) Name()``).
  * ``imports`` for every import spec inside an ``import`` block or
    standalone ``import "x"``. Aliased and dot imports map to ``*``.
  * ``calls`` for every call expression at the body level.

Module path = the import-path-like value derived from the file path's
directory (Go's logical unit). Receiver name becomes the namespace for
methods.
"""

from __future__ import annotations

import logging
from typing import Iterator, List, Optional

import tree_sitter_go as tsg
from tree_sitter import Language, Node, Parser

from .base import Fact, make_symbol_id, register_language

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tsg.language())
_PARSER = Parser(_LANGUAGE)


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: Node, field: str) -> Optional[Node]:
    return node.child_by_field_name(field)


def _module_path(file_path: str) -> str:
    cleaned = file_path.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if "/" in cleaned:
        return cleaned.rsplit("/", 1)[0]
    return cleaned


def _string_lit_value(node: Node, source: bytes) -> Optional[str]:
    if node.type != "interpreted_string_literal":
        return None
    for child in node.children:
        if child.type == "interpreted_string_literal_content":
            return _node_text(child, source)
    return None


def _emit_imports(
    node: Node, source: bytes, file_path: str, module_symbol_id: str
) -> Iterator[Fact]:
    # ``import "x"`` directly, or ``import ( "x" "y" )``.
    for child in node.children:
        if child.type == "import_spec":
            yield from _emit_one_import_spec(
                child, source, file_path, module_symbol_id
            )
        elif child.type == "import_spec_list":
            for spec in child.children:
                if spec.type == "import_spec":
                    yield from _emit_one_import_spec(
                        spec, source, file_path, module_symbol_id
                    )


def _emit_one_import_spec(
    spec: Node, source: bytes, file_path: str, module_symbol_id: str
) -> Iterator[Fact]:
    path_node: Optional[Node] = None
    for child in spec.children:
        if child.type == "interpreted_string_literal":
            path_node = child
            break
    if path_node is None:
        return
    path = _string_lit_value(path_node, source)
    if path is None:
        return
    yield Fact(
        kind="imports",
        source_symbol_id=module_symbol_id,
        target_symbol_id=make_symbol_id("go", path, "*"),
        file_path=file_path,
        line=spec.start_point[0] + 1,
        language="go",
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
    if fn_node.type == "selector_expression":
        op = _child_by_field(fn_node, "operand")
        field = _child_by_field(fn_node, "field")
        if field is None:
            return None
        field_name = _node_text(field, source)
        if op is not None and op.type == "identifier":
            return f"{_node_text(op, source)}.{field_name}"
        return field_name
    return None


def _emit_function_or_method(
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

    receiver_type: Optional[str] = None
    if fn_node.type == "method_declaration":
        receiver = _child_by_field(fn_node, "receiver")
        if receiver is not None:
            # receiver -> parameter_list -> parameter_declaration -> type_identifier
            for n in _flatten(receiver):
                if n.type in ("type_identifier", "pointer_type"):
                    if n.type == "pointer_type":
                        # Drill into pointer type to find type_identifier
                        for inner in _flatten(n):
                            if inner.type == "type_identifier":
                                receiver_type = _node_text(inner, source)
                                break
                    else:
                        receiver_type = _node_text(n, source)
                    if receiver_type:
                        break

    if receiver_type:
        fn_id = make_symbol_id("go", module, name, namespace=receiver_type)
        owner_id = make_symbol_id("go", module, receiver_type)
    else:
        fn_id = make_symbol_id("go", module, name)
        owner_id = module_symbol_id

    yield Fact(
        kind="defines",
        source_symbol_id=owner_id,
        target_symbol_id=fn_id,
        file_path=file_path,
        line=fn_node.start_point[0] + 1,
        language="go",
    )

    body = _child_by_field(fn_node, "body")
    if body is None:
        return
    for callee, line in _walk_calls(body, source):
        yield Fact(
            kind="calls",
            source_symbol_id=fn_id,
            target_symbol_id=make_symbol_id("go", module, callee),
            file_path=file_path,
            line=line,
            language="go",
        )


def _flatten(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _flatten(child)


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Yield Facts for a Go source file."""

    try:
        tree = _PARSER.parse(source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("go tree-sitter parse failed for %s: %s", file_path, exc)
        return

    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("go", module, "<module>")

    for child in tree.root_node.children:
        if child.type == "import_declaration":
            yield from _emit_imports(child, source, file_path, module_symbol_id)
        elif child.type in ("function_declaration", "method_declaration"):
            yield from _emit_function_or_method(
                child, source, file_path, module, module_symbol_id
            )


register_language("go", extract)
