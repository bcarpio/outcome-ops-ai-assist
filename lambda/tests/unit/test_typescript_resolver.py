"""Unit tests for the TypeScript cross-module reference resolver (ADR-055).

Black-box over the TS extractor + the language-agnostic resolution pass: extract
two fixture files, concatenate their facts, run ``resolve_repo_facts``, and assert
that cross-module references (via relative-path named imports) rebind to their
definition ids while unresolvable references stay raw (never-guess).
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
    os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "extractor", "typescript"
    )
)


def _load(name):
    return list(extract_facts(os.path.join(FIXTURE_DIR, name), "typescript"))


def _one(facts, kind, pred):
    hits = [f for f in facts if f.kind == kind and pred(f)]
    assert len(hits) == 1, f"expected exactly one {kind} match, got {len(hits)}"
    return hits[0]


class TestTypescriptResolutionFixtures:
    @pytest.fixture
    def facts(self):
        return _load("resolve_defs.ts") + _load("resolve_caller.ts")

    @pytest.fixture
    def resolved(self, facts):
        return resolve_repo_facts(facts)

    @pytest.fixture
    def target_fn_def(self, resolved):
        return _one(
            resolved,
            "defines",
            lambda f: f.target_symbol_id.endswith("resolve_defs::targetFn"),
        ).target_symbol_id

    @pytest.fixture
    def widget_def(self, resolved):
        return _one(
            resolved,
            "defines",
            lambda f: f.target_symbol_id.endswith("resolve_defs::Widget"),
        ).target_symbol_id

    def test_named_import_call_resolves(self, resolved, target_fn_def):
        """`targetFn(5)` in resolve_caller binds to the definition in
        resolve_defs via the ./resolve_defs named import."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.source_symbol_id.endswith("::caller"),
        )
        assert call.target_symbol_id == target_fn_def
        assert "resolve_defs" in call.target_symbol_id
        assert "resolve_caller::targetFn" not in call.target_symbol_id

    def test_import_resolves_to_definition(self, resolved, target_fn_def):
        """`import { targetFn }` binds to the targetFn definition (importers)."""
        imp = _one(
            resolved,
            "imports",
            lambda f: f.target_symbol_id.endswith("resolve_defs::targetFn"),
        )
        assert imp.target_symbol_id == target_fn_def

    def test_extends_resolves_via_named_import(self, resolved, widget_def):
        """`SubWidget extends Widget` binds to the imported Widget definition."""
        ext = _one(resolved, "extends", lambda f: True)
        assert ext.target_symbol_id == widget_def

    def test_unresolvable_call_left_raw(self, resolved):
        """undefinedHelper() is neither imported nor defined -> raw."""
        call = _one(
            resolved,
            "calls",
            lambda f: f.target_symbol_id.endswith("::undefinedHelper"),
        )
        assert "resolve_caller" in call.target_symbol_id

    def test_resolution_is_idempotent(self, facts):
        once = resolve_repo_facts(facts)
        twice = resolve_repo_facts(once)
        key = lambda fs: sorted(
            (f.kind, f.source_symbol_id, f.target_symbol_id) for f in fs
        )
        assert key(once) == key(twice)


# ADR-056 §7: TS cross-repo binding via non-relative (package) specifiers.
# A TS module id is a file path, so a package import cannot be byte-identical to
# the producer's definition id; it binds by the specifier's last segment matching
# a producer module's basename, with never-guess on ambiguity.
class TestTypescriptCrossRepo:
    from extractor.base import Fact  # type: ignore

    def _consumer_facts(self, specifier):
        F = self.Fact
        return [
            F("defines", "typescript:app/service::<module>",
              "typescript:app/service::process", "app/service.ts", 3, "typescript"),
            F("imports", "typescript:app/service::<module>",
              f"typescript:{specifier}::add", "app/service.ts", 1, "typescript"),
            F("calls", "typescript:app/service::process",
              "typescript:app/service::add", "app/service.ts", 4, "typescript"),
        ]

    def _producer_define(self, module="src/mathx"):
        return self.Fact(
            "defines", f"typescript:{module}::<module>",
            f"typescript:{module}::add", f"{module}.ts", 2, "typescript")

    def _resolved(self, consumer, producers):
        out = resolve_repo_facts(consumer, extra_definitions=producers)
        return {f.kind: f for f in out}

    def test_subpath_package_import_binds_cross_repo(self):
        # import { add } from "ts-shared/mathx"  ->  producer src/mathx::add
        res = self._resolved(
            self._consumer_facts("ts-shared/mathx"), [self._producer_define()])
        assert res["calls"].target_symbol_id == "typescript:src/mathx::add"
        assert res["imports"].target_symbol_id == "typescript:src/mathx::add"

    def test_scoped_package_subpath_binds(self):
        # import { add } from "@acme/lib/mathx"
        res = self._resolved(
            self._consumer_facts("@acme/lib/mathx"), [self._producer_define()])
        assert res["calls"].target_symbol_id == "typescript:src/mathx::add"

    def test_ambiguous_basename_stays_raw(self):
        # Two in-scope modules basename "mathx" both define add -> never-guess.
        producers = [self._producer_define("src/mathx"),
                     self._producer_define("lib/mathx")]
        res = self._resolved(self._consumer_facts("ts-shared/mathx"), producers)
        assert res["calls"].target_symbol_id == "typescript:app/service::add"  # raw

    def test_external_package_without_matching_module_stays_raw(self):
        # import { add } from "lodash" -> no module basename "lodash" -> raw.
        res = self._resolved(
            self._consumer_facts("lodash"), [self._producer_define("src/mathx")])
        assert res["calls"].target_symbol_id == "typescript:app/service::add"  # raw
