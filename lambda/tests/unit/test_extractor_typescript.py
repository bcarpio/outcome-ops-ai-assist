"""Unit tests for the TypeScript extractor."""

from __future__ import annotations

import os
import sys

import pytest

EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor import extract_facts  # noqa: E402


FIXTURE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "extractor", "typescript"
    )
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "typescript"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    @pytest.fixture
    def facts(self):
        return _load("all_edges.ts")

    def test_imports_named_and_namespace(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert any("./helpers::helper" in t for t in targets)
        assert any("pino::*" in t for t in targets)

    def test_class_extends_and_implements(self, facts):
        extends = _by_kind(facts, "extends")
        impls = _by_kind(facts, "implements")
        assert any("BaseService" in f.target_symbol_id for f in extends)
        assert any("IService" in f.target_symbol_id for f in impls)

    def test_class_and_method_defines(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any(i.endswith("::Service") for i in ids)
        assert any(i.endswith("::Service::run") for i in ids)
        assert any(i.endswith("::topLevel") for i in ids)
        assert any(i.endswith("::arrowFn") for i in ids)

    def test_calls_include_self_and_named(self, facts):
        calls = _by_kind(facts, "calls")
        names = [f.target_symbol_id for f in calls]
        assert any("helper" in n for n in names)
        # ``this.log()`` -> emits unresolved 'log'
        assert any(n.endswith("::log") for n in names)


class TestSubpathImports:
    """The @hyatt-hpe/hpe-utils/http2 subpath-import case.

    The plan calls this out explicitly — RAG cannot retrieve consumers
    of a subpath import because there's no prose document. Graph
    retrieval must preserve the full module specifier.
    """

    @pytest.fixture
    def facts(self):
        return _load("subpath_imports.ts")

    def test_http2_subpath_preserved(self, facts):
        targets = [
            f.target_symbol_id
            for f in _by_kind(facts, "imports")
        ]
        assert any("@hyatt-hpe/hpe-utils/http2::http2Client" in t for t in targets)
        assert any("@hyatt-hpe/hpe-utils/logging::logger" in t for t in targets)

    def test_side_effect_import_emits_wildcard(self, facts):
        targets = [
            f.target_symbol_id for f in _by_kind(facts, "imports")
        ]
        assert any("./side-effect-only::*" in t for t in targets)


class TestNameCollisions:
    @pytest.fixture
    def facts(self):
        return _load("name_collisions.ts")

    def test_class_methods_namespaced(self, facts):
        defines = _by_kind(facts, "defines")
        ids = {f.target_symbol_id for f in defines}
        # Both A::helper and B::helper must coexist.
        a_helpers = [i for i in ids if "A::helper" in i]
        b_helpers = [i for i in ids if "B::helper" in i]
        assert a_helpers and b_helpers


class TestSignatures:
    """signatures.ts -- TS function/method/arrow signatures."""

    @pytest.fixture
    def defines(self):
        facts = list(extract_facts(os.path.join(FIXTURE_DIR, "signatures.ts"), "typescript"))
        return {f.target_symbol_id: f for f in facts if f.kind == "defines"}

    def _find(self, defines, suffix):
        return next((f for s, f in defines.items() if s.endswith(suffix)), None)

    def test_bare_function(self, defines):
        f = self._find(defines, "::bare")
        assert f is not None and f.signature is not None
        names = [p.name for p in f.signature.params]
        assert names == ["a", "b"]
        assert all(p.type is None for p in f.signature.params)
        assert f.signature.returns is None

    def test_typed_function(self, defines):
        f = self._find(defines, "::typed")
        assert f is not None and f.signature is not None
        assert [(p.name, p.type) for p in f.signature.params] == [
            ("a", "number"),
            ("b", "string"),
        ]
        assert f.signature.returns == "boolean"

    def test_default_keeps_type_drops_default(self, defines):
        f = self._find(defines, "::defaulted")
        assert f is not None and f.signature is not None
        assert [(p.name, p.type) for p in f.signature.params] == [
            ("a", "number"),
            ("b", "string"),
        ]
        assert f.signature.returns == "number | undefined"

    def test_method_with_optional(self, defines):
        f = self._find(defines, "Greeter::greet")
        assert f is not None and f.signature is not None
        names = [p.name for p in f.signature.params]
        # loud is marked optional and surfaces with the `?` suffix.
        assert names == ["name", "loud?"]
        assert f.signature.returns == "string"

    def test_arrow_function_signature(self, defines):
        f = self._find(defines, "::arrow")
        assert f is not None and f.signature is not None
        assert [(p.name, p.type) for p in f.signature.params] == [
            ("name", "string"),
            ("loud", "boolean"),
        ]
        assert f.signature.returns == "string"

    def test_class_define_has_no_signature(self, defines):
        f = self._find(defines, "::Greeter")
        assert f is not None
        assert f.signature is None
