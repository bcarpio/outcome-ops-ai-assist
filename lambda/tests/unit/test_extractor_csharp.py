"""Unit tests for the C# extractor (plan-gkg chunk 01, ADR-017 regex strategy)."""

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
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "csharp")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "csharp"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    @pytest.fixture
    def facts(self):
        return _load("AllEdges.cs")

    def test_namespace_module(self, facts):
        defines = _by_kind(facts, "defines")
        assert any("Hyatt.Booking" in f.source_symbol_id for f in defines)

    def test_usings_emit_imports(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert any("System::*" in t for t in targets)
        assert any("Hyatt::Util" in t for t in targets)

    def test_first_base_is_extends_rest_are_implements(self, facts):
        extends = _by_kind(facts, "extends")
        impls = _by_kind(facts, "implements")
        assert any("BaseService" in f.target_symbol_id for f in extends)
        assert any("IBookable" in f.target_symbol_id for f in impls)

    def test_method_defines_and_calls(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any("BookingService::Confirm" in i for i in ids)
        assert any("BookingService::NotifyOps" in i for i in ids)
        calls = _by_kind(facts, "calls")
        assert any(f.target_symbol_id.endswith("::Log") for f in calls)
        assert any(f.target_symbol_id.endswith("::NotifyOps") for f in calls)

    def test_keywords_not_called(self, facts):
        calls = _by_kind(facts, "calls")
        for f in calls:
            assert not f.target_symbol_id.endswith("::if")
            assert not f.target_symbol_id.endswith("::for")
            assert not f.target_symbol_id.endswith("::return")


class TestNameCollisions:
    @pytest.fixture
    def facts(self):
        return _load("NameCollisions.cs")

    def test_helper_namespaced_per_class(self, facts):
        defines = _by_kind(facts, "defines")
        ids = {f.target_symbol_id for f in defines}
        a_helpers = [i for i in ids if "A::Helper" in i]
        b_helpers = [i for i in ids if "B::Helper" in i]
        assert a_helpers
        assert b_helpers
