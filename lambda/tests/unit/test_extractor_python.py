# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Python extractor."""

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
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "python")
)


def _load(name: str):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "python"))


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


class TestAllEdges:
    """all_edges.py exercises every Python edge kind."""

    @pytest.fixture
    def facts(self):
        return _load("all_edges.py")

    def test_imports_present(self, facts):
        imports = _by_kind(facts, "imports")
        targets = [f.target_symbol_id for f in imports]
        assert "python:os::*" in targets
        assert "python:typing::List" in targets
        assert "python:helpers::normalize" in targets

    def test_class_and_method_defines(self, facts):
        defines = _by_kind(facts, "defines")
        ids = [f.target_symbol_id for f in defines]
        assert any(i.endswith("::Service") for i in ids)
        assert any(i.endswith("::Service::run") for i in ids)
        assert any(i.endswith("::top_level") for i in ids)

    def test_extends_edge(self, facts):
        extends = _by_kind(facts, "extends")
        assert any("Base" in f.target_symbol_id for f in extends)

    def test_calls_resolve_self_to_unresolved_name(self, facts):
        calls = _by_kind(facts, "calls")
        # ``self.hello()`` -> emits unresolved name 'hello' per plan
        assert any(f.target_symbol_id.endswith("::hello") for f in calls)

    def test_module_attribute_call_preserved(self, facts):
        calls = _by_kind(facts, "calls")
        assert any("os.getcwd" in f.target_symbol_id for f in calls)


class TestNameCollisions:
    """Same name in different scopes must be namespaced distinctly."""

    @pytest.fixture
    def facts(self):
        return _load("name_collisions.py")

    def test_distinct_method_ids(self, facts):
        ids = {f.target_symbol_id for f in facts if f.kind == "defines"}
        # The module-level ``helper`` and the two class methods must
        # carry different canonical IDs.
        helpers = [i for i in ids if i.endswith("::helper") or "::helper" in i]
        assert len(helpers) >= 3


class TestReportReposSnapshot:
    """The load-bearing v1 success-case fixture.

    The plan's DoD says the extractor must surface 10 distinct callers
    of ``log_audit_entry``. reuses this fixture as the
    live-data signal so the test name and assertion are stable.
    """

    @pytest.fixture
    def facts(self):
        return _load("log_audit_entry_callers.py")

    def test_one_import_of_symbol(self, facts):
        imports = _by_kind(facts, "imports")
        snap_imports = [
            f for f in imports if f.target_symbol_id.endswith("::log_audit_entry")
        ]
        assert len(snap_imports) == 1

    def test_ten_distinct_callers(self, facts):
        calls = _by_kind(facts, "calls")
        snap_calls = [
            f
            for f in calls
            if f.target_symbol_id.endswith("::log_audit_entry")
        ]
        callers = {f.source_symbol_id for f in snap_calls}
        assert len(callers) == 10, sorted(callers)


class TestExtractorBehavior:
    def test_unknown_language_returns_empty(self, tmp_path):
        p = tmp_path / "x.unknown"
        p.write_bytes(b"")
        assert list(extract_facts(str(p), "unknown-lang")) == []

    def test_missing_file_returns_empty(self):
        assert list(extract_facts("/nonexistent/path/x.py", "python")) == []

    def test_excluded_path_short_circuits(self, tmp_path):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        f = node_modules / "x.py"
        f.write_text("def excluded(): pass\n")
        assert list(extract_facts(str(f), "python")) == []

    def test_extract_facts_from_bytes_round_trip(self):
        from extractor import extract_facts_from_bytes

        src = b"def foo():\n    bar()\n"
        facts = list(extract_facts_from_bytes("ex.py", "python", src))
        kinds = {f.kind for f in facts}
        assert "defines" in kinds and "calls" in kinds

    def test_extract_facts_from_bytes_skips_excluded(self):
        from extractor import extract_facts_from_bytes

        # ``node_modules/`` in the path -> excluded by ADR-009 alignment.
        facts = list(
            extract_facts_from_bytes(
                "node_modules/lib/x.py", "python", b"def f(): pass\n"
            )
        )
        assert facts == []

    def test_extract_facts_from_bytes_unknown_lang(self):
        from extractor import extract_facts_from_bytes

        assert list(extract_facts_from_bytes("x.py", "unknown", b"")) == []


class TestSignatures:
    """signatures.py: function/method signatures with varied annotations."""

    @pytest.fixture
    def defines_by_target(self):
        facts = _load("signatures.py")
        return {f.target_symbol_id: f for f in _by_kind(facts, "defines")}

    def _find(self, defines_by_target, suffix):
        return next(
            (f for s, f in defines_by_target.items() if s.endswith(suffix)),
            None,
        )

    def test_bare_params_no_types(self, defines_by_target):
        fact = self._find(defines_by_target, "::bare")
        assert fact is not None and fact.signature is not None
        names = [p.name for p in fact.signature.params]
        assert names == ["a", "b"]
        assert all(p.type is None for p in fact.signature.params)
        assert fact.signature.returns is None

    def test_typed_params_and_return(self, defines_by_target):
        fact = self._find(defines_by_target, "::typed")
        assert fact is not None and fact.signature is not None
        sig = fact.signature
        assert [(p.name, p.type) for p in sig.params] == [
            ("a", "int"),
            ("b", "str"),
        ]
        assert sig.returns == "bool"

    def test_typed_default_keeps_type_drops_default(self, defines_by_target):
        # Defaults are intentionally NOT captured (ADR-027 -- literals
        # stay out). Type annotation IS captured.
        fact = self._find(defines_by_target, "::typed_with_defaults")
        assert fact is not None and fact.signature is not None
        assert [(p.name, p.type) for p in fact.signature.params] == [
            ("a", "int"),
            ("b", "str"),
        ]
        assert fact.signature.returns == "Optional[int]"

    def test_varargs_and_kwargs_emit_spelling(self, defines_by_target):
        fact = self._find(defines_by_target, "::varargs")
        assert fact is not None and fact.signature is not None
        names = [p.name for p in fact.signature.params]
        assert names == ["*args", "**kwargs"]
        assert fact.signature.returns == "List[int]"

    def test_method_signature_includes_self(self, defines_by_target):
        fact = self._find(defines_by_target, "Greeter::greet")
        assert fact is not None and fact.signature is not None
        names = [p.name for p in fact.signature.params]
        assert names == ["self", "name", "loud"]
        types = {p.name: p.type for p in fact.signature.params}
        assert types["self"] is None
        assert types["name"] == "str"
        assert types["loud"] == "bool"
        assert fact.signature.returns == "str"

    def test_class_define_has_no_signature(self, defines_by_target):
        # Class itself is a `defines` fact but is not a callable; the
        # extractor should leave the signature field None.
        fact = self._find(defines_by_target, "::Greeter")
        assert fact is not None
        assert fact.signature is None


class TestDecoratedDefinitions:
    """Decorated top-level defs must NOT be hidden by the
    ``decorated_definition`` wrapper (the ADR-046 extractor gap)."""

    @pytest.fixture
    def facts(self):
        return _load("decorated.py")

    def _define_ids(self, facts):
        return [f.target_symbol_id for f in _by_kind(facts, "defines")]

    def test_decorated_function_defines_and_calls(self, facts):
        ids = self._define_ids(facts)
        assert any(i.endswith("::list_items") for i in ids)
        # @app.get-decorated route still emits its internal call.
        calls = _by_kind(facts, "calls")
        assert any(
            "list_items" in f.source_symbol_id
            and f.target_symbol_id.endswith("::authorize")
            for f in calls
        )

    def test_decorated_class_defines_and_extends(self, facts):
        ids = self._define_ids(facts)
        assert any(i.endswith("::Record") for i in ids)
        extends = _by_kind(facts, "extends")
        assert any("Base" in f.target_symbol_id for f in extends)

    def test_decorated_method_defines_and_calls(self, facts):
        ids = self._define_ids(facts)
        # @staticmethod / @property decorated methods surface with their
        # class namespace.
        assert any(i.endswith("::Controller::handle") for i in ids)
        assert any(i.endswith("::Controller::name") for i in ids)
        calls = _by_kind(facts, "calls")
        assert any(
            "Controller::handle" in f.source_symbol_id
            and f.target_symbol_id.endswith("::authorize")
            for f in calls
        )
        assert any(
            "Controller::name" in f.source_symbol_id
            and f.target_symbol_id.endswith("::compute_name")
            for f in calls
        )

    def test_undecorated_regression(self, facts):
        ids = self._define_ids(facts)
        assert any(i.endswith("::undecorated") for i in ids)

    def test_decorator_itself_not_emitted_as_call(self, facts):
        # @app.get("/items") is a call expression but decorators are out of
        # scope — no `calls` fact should target app.get.
        calls = _by_kind(facts, "calls")
        assert not any(
            f.target_symbol_id.endswith("app.get")
            or f.target_symbol_id.endswith("::get")
            for f in calls
        )
