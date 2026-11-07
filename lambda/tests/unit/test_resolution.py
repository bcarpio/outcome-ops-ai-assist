"""Unit tests for cross-module reference resolution (ADR-055).

Black-box over the extractor + the language-agnostic resolution pass:
extract two fixture files, concatenate their facts, run ``resolve_repo_facts``,
and assert that cross-file references are rebound to their definition ids while
unresolvable / ambiguous references are left raw (never-guess).
"""

from __future__ import annotations

import os
import sys

import pytest

EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor import (  # noqa: E402
    build_definitions_index,
    extract_facts,
    resolve_repo_facts,
)
from extractor.base import Fact, make_symbol_id  # noqa: E402

FIXTURE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "python")
)


def _load(name):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "python"))


def _one(facts, kind, pred):
    hits = [f for f in facts if f.kind == kind and pred(f)]
    assert len(hits) == 1, f"expected exactly one {kind} match, got {len(hits)}"
    return hits[0]


class TestPythonResolutionFixtures:
    @pytest.fixture
    def resolved(self):
        facts = _load("resolve_defs.py") + _load("resolve_caller.py")
        return resolve_repo_facts(facts)

    @pytest.fixture
    def target_fn_def(self, resolved):
        return _one(
            resolved, "defines", lambda f: f.target_symbol_id.endswith("::target_fn")
        ).target_symbol_id

    @pytest.fixture
    def widget_def(self, resolved):
        return _one(
            resolved, "defines", lambda f: f.target_symbol_id.endswith("::Widget")
        ).target_symbol_id

    def test_cross_file_call_resolves_to_definition(self, resolved, target_fn_def):
        call = _one(
            resolved,
            "calls",
            lambda f: f.source_symbol_id.endswith("::caller"),
        )
        # Before resolution this pointed at the CALLER's module namespace; it
        # must now point at the definition in resolve_defs.
        assert call.target_symbol_id == target_fn_def
        assert "resolve_defs" in call.target_symbol_id
        assert "resolve_caller" not in call.target_symbol_id

    def test_import_resolves_to_definition(self, resolved, target_fn_def):
        imp = _one(
            resolved,
            "imports",
            lambda f: f.target_symbol_id.endswith("::target_fn"),
        )
        assert imp.target_symbol_id == target_fn_def

    def test_extends_resolves_to_base_definition(self, resolved, widget_def):
        ext = _one(resolved, "extends", lambda f: True)
        assert ext.target_symbol_id == widget_def

    def test_unresolvable_call_left_raw(self, resolved):
        # undefined_helper() is neither imported nor defined anywhere -> raw.
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::undefined_helper"),
        )
        # Still in the caller's own namespace; never bound/guessed.
        assert "resolve_caller" in call.target_symbol_id

    def test_definitions_pass_through_unchanged(self):
        raw = _load("resolve_defs.py")
        resolved = resolve_repo_facts(raw + _load("resolve_caller.py"))
        raw_def_ids = {f.target_symbol_id for f in raw if f.kind == "defines"}
        resolved_def_ids = {f.target_symbol_id for f in resolved if f.kind == "defines"}
        assert raw_def_ids <= resolved_def_ids


class TestResolutionFramework:
    def _def(self, module, name, path):
        return Fact(
            kind="defines",
            source_symbol_id=make_symbol_id("python", module, "<module>"),
            target_symbol_id=make_symbol_id("python", module, name),
            file_path=path,
            line=1,
            language="python",
        )

    def _call(self, caller_module, caller, callee, path):
        return Fact(
            kind="calls",
            source_symbol_id=make_symbol_id("python", caller_module, caller),
            target_symbol_id=make_symbol_id("python", caller_module, callee),
            file_path=path,
            line=2,
            language="python",
        )

    def test_ambiguous_name_left_raw(self):
        # Same name defined in two modules; a call with no import must NOT guess.
        facts = [
            self._def("pkg.a", "amb", "a.py"),
            self._def("pkg.b", "amb", "b.py"),
            self._call("pkg.c", "c_fn", "amb", "c.py"),
        ]
        resolved = resolve_repo_facts(facts)
        call = _one(resolved, "calls", lambda f: True)
        assert call.target_symbol_id == make_symbol_id("python", "pkg.c", "amb")

    def test_unique_name_fallback_resolves(self):
        facts = [
            self._def("pkg.a", "only_one", "a.py"),
            self._call("pkg.c", "c_fn", "only_one", "c.py"),
        ]
        resolved = resolve_repo_facts(facts)
        call = _one(resolved, "calls", lambda f: True)
        assert call.target_symbol_id == make_symbol_id("python", "pkg.a", "only_one")

    def test_language_without_resolver_passthrough(self):
        # 'ruby' has no registered resolver -> facts unchanged.
        facts = [
            Fact(
                kind="defines",
                source_symbol_id="ruby:pkg::<module>",
                target_symbol_id="ruby:pkg::thing",
                file_path="x.rb",
                line=1,
                language="ruby",
            ),
            Fact(
                kind="calls",
                source_symbol_id="ruby:other::caller",
                target_symbol_id="ruby:other::thing",
                file_path="y.rb",
                line=2,
                language="ruby",
            ),
        ]
        resolved = resolve_repo_facts(facts)
        call = _one(resolved, "calls", lambda f: True)
        assert call.target_symbol_id == "ruby:other::thing"

    def test_build_definitions_index(self):
        facts = [
            self._def("pkg.a", "foo", "a.py"),
            self._def("pkg.b", "foo", "b.py"),
            self._def("pkg.a", "bar", "a.py"),
        ]
        idx = build_definitions_index(facts)
        assert ("pkg.a", "foo") in idx.by_qualified
        assert len(idx.by_name["foo"]) == 2
        assert {"pkg.a", "pkg.b"} <= idx.modules
