# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Go cross-module reference resolver (ADR-055).

Black-box over the Go extractor + the language-agnostic resolution pass: extract
two fixture packages (different directories), concatenate their facts, run
``resolve_repo_facts``, and assert that a cross-package selector call rebinds to
the callee package's definition while bare/unresolvable calls stay raw
(never-guess).
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

from extractor import extract_facts, resolve_repo_facts  # noqa: E402

FIXTURE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "extractor", "go")
)


def _load(relpath):
    return list(extract_facts(os.path.join(FIXTURE_DIR, relpath), "go"))


def _one(facts, kind, pred):
    hits = [f for f in facts if f.kind == kind and pred(f)]
    assert len(hits) == 1, f"expected exactly one {kind} match, got {len(hits)}"
    return hits[0]


class TestGoResolutionFixtures:
    @pytest.fixture
    def facts(self):
        return _load("widget/widget.go") + _load("app/app.go")

    @pytest.fixture
    def resolved(self, facts):
        return resolve_repo_facts(facts)

    @pytest.fixture
    def target_fn_def(self, resolved):
        return _one(
            resolved,
            "defines",
            lambda f: f.target_symbol_id.endswith("widget::TargetFn"),
        ).target_symbol_id

    def test_cross_package_selector_call_resolves(self, resolved, target_fn_def):
        """`widget.TargetFn(5)` in the app package binds to the definition in the
        widget package."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.source_symbol_id.endswith("app::Caller"),
        )
        assert call.target_symbol_id == target_fn_def
        assert call.target_symbol_id.endswith("widget::TargetFn")
        assert "app::widget.TargetFn" not in call.target_symbol_id

    def test_bare_unresolvable_call_left_raw(self, resolved):
        """undefinedHelper() is a bare call with no same-package definition and no
        selector -> left raw (never-guess; no unique-name guessing for Go)."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::undefinedHelper"),
        )
        assert call.target_symbol_id.endswith("app::undefinedHelper")

    def test_package_import_left_raw(self, resolved):
        """A Go package import names no single definition -> raw."""
        imp = _one(resolved, "imports", lambda f: True)
        assert imp.target_symbol_id == "go:example.com/proj/widget::*"

    def test_resolution_is_idempotent(self, facts):
        once = resolve_repo_facts(facts)
        twice = resolve_repo_facts(once)
        key = lambda fs: sorted(
            (f.kind, f.source_symbol_id, f.target_symbol_id) for f in fs
        )
        assert key(once) == key(twice)
