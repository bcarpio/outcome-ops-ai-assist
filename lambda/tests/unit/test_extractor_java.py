"""Unit tests for the Java extractor (plan-gkg chunk 01)."""

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
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "java")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "java"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    @pytest.fixture
    def facts(self):
        return _load("AllEdges.java")

    def test_package_in_module(self, facts):
        defines = _by_kind(facts, "defines")
        assert any("com.hyatt.booking" in f.source_symbol_id for f in defines)

    def test_imports_split_by_last_dot(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert "java:com.hyatt.util::Logger" in targets
        assert "java:com.hyatt.util::Helper" in targets
        assert "java:java.util::List" in targets

    def test_class_extends_and_implements(self, facts):
        extends = _by_kind(facts, "extends")
        impls = _by_kind(facts, "implements")
        assert any("BaseService" in f.target_symbol_id for f in extends)
        assert {f.target_symbol_id for f in impls} == {
            "java:IBookable::*",
            "java:Reportable::*",
        }

    def test_method_defines_and_calls(self, facts):
        defines = _by_kind(facts, "defines")
        method_ids = [f for f in defines if "BookingService::" in f.target_symbol_id]
        assert any("confirm" in f.target_symbol_id for f in method_ids)
        assert any("notifyOps" in f.target_symbol_id for f in method_ids)

        calls = _by_kind(facts, "calls")
        # ``this.notifyOps()`` resolves to unresolved name 'notifyOps'.
        assert any(f.target_symbol_id.endswith("::notifyOps") for f in calls)


class TestNameCollisions:
    @pytest.fixture
    def facts(self):
        return _load("NameCollisions.java")

    def test_helper_method_defined_and_called(self, facts):
        defines = _by_kind(facts, "defines")
        calls = _by_kind(facts, "calls")
        assert any(f.target_symbol_id.endswith("::helper") for f in defines)
        assert any(f.target_symbol_id.endswith("::helper") for f in calls)
