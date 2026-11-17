# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Python symbol/edge extractor backed by Tree-sitter.

Emits:

  * ``defines`` for every top-level function and class, plus methods
    defined inside a top-level class. Locals/closures are intentionally
    skipped (low-signal, high-noise per the chunk-01 scope).
  * ``imports`` for every ``import x`` / ``from x import y`` /
    ``from x import *``. ``from x import y, z`` emits one fact per name.
  * ``calls`` for every call expression at statement level. ``self.foo()``
    style calls emit the unresolved name (per the open question in the
    plan — the query layer does best-effort caller matching).
  * ``extends`` for every ``class X(Y)``.

Module path canonicalization: ``lambda/chat/handler.py`` becomes
``lambda.chat.handler``. Leading ``./`` is stripped.
"""

from __future__ import annotations

import logging
from typing import Iterator, List, Optional

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from .base import Fact, Param, Signature, make_symbol_id, register_language

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tsp.language())
_PARSER = Parser(_LANGUAGE)


def _module_path(file_path: str) -> str:
    """Convert a repo-relative file path to a Python-style module path."""

    cleaned = file_path.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.endswith(".py"):
        cleaned = cleaned[:-3]
    return cleaned.replace("/", ".")


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: Node, field: str) -> Optional[Node]:
    return node.child_by_field_name(field)


def _extract_signature(func_node: Node, source: bytes) -> Signature:
    """Build a ``Signature`` from a tree-sitter ``function_definition`` node.

    Captures parameter names + their type annotations (if present) and the
    return type annotation (if present). Defaults are intentionally NOT
    captured per ADR-027 — literals stay out of the graph. ``*args``,
    ``**kwargs``, and ``/`` / ``*`` separators are surfaced under their
    spelling (``*args``, ``**kwargs``, ``*``, ``/``) so callers can see the
    full positional/keyword shape without inferring it.
    """

    params: List[Param] = []
    parameters_node = _child_by_field(func_node, "parameters")
    if parameters_node is not None:
        for child in parameters_node.children:
            t = child.type
            if t == "identifier":
                params.append(Param(name=_node_text(child, source)))
            elif t == "typed_parameter":
                # tree-sitter-python's typed_parameter exposes a `type` field
                # but the name is a positional identifier child, not a field.
                name_node = next(
                    (c for c in child.children if c.type == "identifier"),
                    None,
                )
                type_node = _child_by_field(child, "type")
                if name_node is not None:
                    params.append(
                        Param(
                            name=_node_text(name_node, source),
                            type=_node_text(type_node, source) if type_node else None,
                        )
                    )
            elif t == "default_parameter":
                name_node = _child_by_field(child, "name")
                if name_node is not None:
                    params.append(Param(name=_node_text(name_node, source)))
            elif t == "typed_default_parameter":
                name_node = _child_by_field(child, "name")
                type_node = _child_by_field(child, "type")
                if name_node is not None:
                    params.append(
                        Param(
                            name=_node_text(name_node, source),
                            type=_node_text(type_node, source) if type_node else None,
                        )
                    )
            elif t == "list_splat_pattern":
                # *args — emit the spelling, including the star.
                params.append(Param(name=_node_text(child, source)))
            elif t == "dictionary_splat_pattern":
                # **kwargs — same.
                params.append(Param(name=_node_text(child, source)))
            elif t in ("*", "/"):
                # Positional-only / keyword-only separator. Surface the
                # spelling so the signature reads as-written.
                params.append(Param(name=t))
            # Other types ('(', ')', ',') are syntactic and skipped.

    returns: Optional[str] = None
    return_type_node = _child_by_field(func_node, "return_type")
    if return_type_node is not None:
        returns = _node_text(return_type_node, source)

    return Signature(params=tuple(params), returns=returns)


def _walk_calls(node: Node, source: bytes) -> Iterator[tuple[str, int]]:
    """Yield (callee_name, line) for every call descendant of ``node``."""

    stack: List[Node] = list(node.children)
    while stack:
        current = stack.pop()
        if current.type == "call":
            fn = _child_by_field(current, "function")
            if fn is not None:
                name = _callee_name(fn, source)
                if name:
                    yield name, current.start_point[0] + 1
        stack.extend(current.children)


def _callee_name(fn_node: Node, source: bytes) -> Optional[str]:
    """Resolve a call's function node to a name.

    ``foo()``         -> ``foo``
    ``mod.sub.foo()`` -> ``mod.sub.foo``
    ``self.foo()``    -> ``foo``  (per the plan's open question — emit
                                   the unresolved name and let the query
                                   layer do best-effort matching)
    ``obj.foo()``     -> ``foo``
    """

    if fn_node.type == "identifier":
        return _node_text(fn_node, source)
    if fn_node.type == "attribute":
        attr = _child_by_field(fn_node, "attribute")
        obj = _child_by_field(fn_node, "object")
        if attr is None:
            return None
        attr_name = _node_text(attr, source)
        if obj is not None:
            obj_text = _node_text(obj, source)
            # ``self.foo`` -> ``foo``; ``mod.sub.foo`` -> ``mod.sub.foo``
            if obj_text == "self" or obj_text == "cls":
                return attr_name
            # Heuristic: dotted access on a name-like expression is preserved
            # whole. Anything else (calls, subscripts) returns just the attr.
            if all(part.isidentifier() for part in obj_text.split(".")):
                return f"{obj_text}.{attr_name}"
        return attr_name
    return None


def _emit_imports(
    node: Node, source: bytes, file_path: str, source_id: str
) -> Iterator[Fact]:
    line = node.start_point[0] + 1
    if node.type == "import_statement":
        # ``import a, b.c``
        for child in node.children:
            if child.type == "dotted_name" or child.type == "aliased_import":
                if child.type == "aliased_import":
                    name_node = _child_by_field(child, "name")
                    if name_node is None:
                        continue
                    module = _node_text(name_node, source)
                else:
                    module = _node_text(child, source)
                yield Fact(
                    kind="imports",
                    source_symbol_id=source_id,
                    target_symbol_id=make_symbol_id("python", module, "*"),
                    file_path=file_path,
                    line=line,
                    language="python",
                )
    elif node.type == "import_from_statement":
        module_node = _child_by_field(node, "module_name")
        if module_node is None:
            return
        module = _node_text(module_node, source)
        # ``from x import *``
        wildcard = any(c.type == "wildcard_import" for c in node.children)
        if wildcard:
            yield Fact(
                kind="imports",
                source_symbol_id=source_id,
                target_symbol_id=make_symbol_id("python", module, "*"),
                file_path=file_path,
                line=line,
                language="python",
            )
            return
        # Named imports
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                if child == module_node:
                    continue
                if child.type == "aliased_import":
                    name_node = _child_by_field(child, "name")
                    if name_node is None:
                        continue
                    name = _node_text(name_node, source)
                else:
                    name = _node_text(child, source)
                yield Fact(
                    kind="imports",
                    source_symbol_id=source_id,
                    target_symbol_id=make_symbol_id("python", module, name),
                    file_path=file_path,
                    line=line,
                    language="python",
                )


def _emit_class_extends(
    class_node: Node,
    source: bytes,
    file_path: str,
    class_source_id: str,
) -> Iterator[Fact]:
    args = _child_by_field(class_node, "superclasses")
    if args is None:
        return
    for child in args.children:
        if child.type in ("identifier", "attribute"):
            base = _node_text(child, source)
            line = child.start_point[0] + 1
            yield Fact(
                kind="extends",
                source_symbol_id=class_source_id,
                target_symbol_id=make_symbol_id("python", base, "*"),
                file_path=file_path,
                line=line,
                language="python",
            )


def _inner_def(node: Node) -> Optional[Node]:
    """Unwrap a ``decorated_definition`` to its inner function/class node.

    A decorated top-level def (``@app.get(...)``, ``@dataclass`` over a class,
    ``@staticmethod`` over a method) parses as a ``decorated_definition`` node
    that WRAPS the real ``function_definition`` / ``class_definition``. The
    extractor's loops match on the definition node types, so without unwrapping
    a decorated def is skipped entirely — no ``defines`` fact and none of its
    internal ``calls``. FastAPI-style route handlers (``@app.get`` etc.) are the
    common casualty. Returns ``node`` unchanged when it is not decorated, or
    ``None`` when the wrapper has no inner definition. Decorators themselves are
    intentionally NOT emitted as calls (out of scope).
    """

    if node.type != "decorated_definition":
        return node
    inner = _child_by_field(node, "definition")
    if inner is not None:
        return inner
    for c in reversed(node.children):
        if c.type in ("function_definition", "class_definition"):
            return c
    return None


def _emit_function(
    fn_node: Node,
    module: str,
    module_symbol_id: str,
    source: bytes,
    file_path: str,
) -> Iterator[Fact]:
    """Emit the ``defines`` fact for a top-level function plus its body calls.

    ``line`` anchors on the ``def`` keyword (the inner node's start), matching
    the undecorated case — not the decorator line.
    """

    name_node = _child_by_field(fn_node, "name")
    if name_node is None:
        return
    fn_name = _node_text(name_node, source)
    fn_id = make_symbol_id("python", module, fn_name)
    yield Fact(
        kind="defines",
        source_symbol_id=module_symbol_id,
        target_symbol_id=fn_id,
        file_path=file_path,
        line=fn_node.start_point[0] + 1,
        language="python",
        signature=_extract_signature(fn_node, source),
    )
    body = _child_by_field(fn_node, "body")
    if body is not None:
        for callee, line in _walk_calls(body, source):
            yield Fact(
                kind="calls",
                source_symbol_id=fn_id,
                target_symbol_id=make_symbol_id("python", module, callee),
                file_path=file_path,
                line=line,
                language="python",
            )


def _emit_class(
    cls_node: Node,
    module: str,
    module_symbol_id: str,
    source: bytes,
    file_path: str,
) -> Iterator[Fact]:
    """Emit ``defines`` + ``extends`` for a class and its methods (including
    decorated methods, unwrapped via ``_inner_def``)."""

    name_node = _child_by_field(cls_node, "name")
    if name_node is None:
        return
    cls_name = _node_text(name_node, source)
    cls_id = make_symbol_id("python", module, cls_name)
    yield Fact(
        kind="defines",
        source_symbol_id=module_symbol_id,
        target_symbol_id=cls_id,
        file_path=file_path,
        line=cls_node.start_point[0] + 1,
        language="python",
    )
    yield from _emit_class_extends(cls_node, source, file_path, cls_id)
    body = _child_by_field(cls_node, "body")
    if body is None:
        return
    for member in body.children:
        method = _inner_def(member)
        if method is None or method.type != "function_definition":
            continue
        m_name_node = _child_by_field(method, "name")
        if m_name_node is None:
            continue
        m_name = _node_text(m_name_node, source)
        method_id = make_symbol_id("python", module, m_name, namespace=cls_name)
        yield Fact(
            kind="defines",
            source_symbol_id=cls_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=method.start_point[0] + 1,
            language="python",
            signature=_extract_signature(method, source),
        )
        m_body = _child_by_field(method, "body")
        if m_body is None:
            continue
        for callee, line in _walk_calls(m_body, source):
            yield Fact(
                kind="calls",
                source_symbol_id=method_id,
                target_symbol_id=make_symbol_id("python", module, callee),
                file_path=file_path,
                line=line,
                language="python",
            )


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Yield Facts for a single Python source file."""

    try:
        tree = _PARSER.parse(source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("python tree-sitter parse failed for %s: %s", file_path, exc)
        return

    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("python", module, "<module>")

    root = tree.root_node
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            yield from _emit_imports(child, source, file_path, module_symbol_id)
            continue

        # Unwrap decorated top-level defs so @decorator over a def/class is not
        # silently skipped (the pre-fix root loop matched only the bare
        # definition node types).
        node = _inner_def(child)
        if node is None:
            continue
        if node.type == "function_definition":
            yield from _emit_function(
                node, module, module_symbol_id, source, file_path
            )
        elif node.type == "class_definition":
            yield from _emit_class(
                node, module, module_symbol_id, source, file_path
            )


register_language("python", extract)
