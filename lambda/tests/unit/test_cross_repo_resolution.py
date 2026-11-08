"""Unit tests for cross-repo reference resolution (ADR-056).

The workspace-wide definitions index: ``resolve_repo_facts(facts,
extra_definitions=...)`` resolves one repo's references against its own defines
PLUS sibling-repo defines, so a cross-repo call binds to the producer's
definition. Sibling defines are lookup-only (never rewritten or emitted), and
never-guess still holds workspace-wide.
"""

from __future__ import annotations

import os
import sys

EXTRACTOR_PARENT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "process-batch-summary")
)
if EXTRACTOR_PARENT not in sys.path:
    sys.path.insert(0, EXTRACTOR_PARENT)

from extractor import resolve_repo_facts  # noqa: E402
from extractor.base import Fact  # noqa: E402


# --- Producer (shared-lib) definition, mirroring the live baseline ------------
PRODUCER_ADD = Fact(
    kind="defines",
    source_symbol_id="python:shared_lib.math_utils::<module>",
    target_symbol_id="python:shared_lib.math_utils::add",
    file_path="shared_lib/math_utils.py",
    line=4,
    language="python",
)

# --- Consumer (consumer-app) facts --------------------------------------------
def _consumer_facts():
    return [
        Fact(
            kind="defines",
            source_symbol_id="python:app.main::<module>",
            target_symbol_id="python:app.main::compute_total",
            file_path="app/main.py",
            line=7,
            language="python",
        ),
        Fact(
            kind="imports",
            source_symbol_id="python:app.main::<module>",
            target_symbol_id="python:shared_lib.math_utils::add",
            file_path="app/main.py",
            line=4,
            language="python",
        ),
        Fact(
            kind="calls",
            source_symbol_id="python:app.main::compute_total",
            target_symbol_id="python:app.main::add",  # raw, consumer namespace
            file_path="app/main.py",
            line=8,
            language="python",
        ),
    ]


def _call_target(facts):
    calls = [f for f in facts if f.kind == "calls"]
    assert len(calls) == 1
    return calls[0].target_symbol_id


class TestCrossRepoResolution:
    def test_cross_repo_call_binds_to_producer_definition(self):
        """With the producer's define in extra_definitions, the consumer's call
        rebinds to the producer definition (cross-repo)."""
        out = resolve_repo_facts(_consumer_facts(), extra_definitions=[PRODUCER_ADD])
        assert _call_target(out) == "python:shared_lib.math_utils::add"

    def test_without_extra_definitions_call_stays_raw(self):
        """Within-repo behavior unchanged: no sibling defines -> the cross-repo
        call cannot bind and stays in the consumer namespace."""
        out = resolve_repo_facts(_consumer_facts())
        assert _call_target(out) == "python:app.main::add"

    def test_sibling_defines_are_not_emitted(self):
        """extra_definitions are lookup-only: the output contains ONLY the
        consumer's own facts, never the producer's defines."""
        out = resolve_repo_facts(_consumer_facts(), extra_definitions=[PRODUCER_ADD])
        assert all("shared_lib.math_utils::add" != f.target_symbol_id or f.kind != "defines"
                   for f in out)
        assert not any(f.file_path == "shared_lib/math_utils.py" for f in out)
        assert len(out) == len(_consumer_facts())

    def test_ambiguous_cross_repo_name_stays_raw(self):
        """Never-guess workspace-wide: a BARE call whose name is defined in two
        sibling repos (no import to disambiguate) does not bind."""
        facts = [
            Fact("defines", "python:app.worker::<module>",
                 "python:app.worker::run", "app/worker.py", 1, "python"),
            Fact("calls", "python:app.worker::run",
                 "python:app.worker::helper", "app/worker.py", 2, "python"),
        ]
        siblings = [
            Fact("defines", "python:libA.util::<module>",
                 "python:libA.util::helper", "libA/util.py", 1, "python"),
            Fact("defines", "python:libB.util::<module>",
                 "python:libB.util::helper", "libB/util.py", 1, "python"),
        ]
        out = resolve_repo_facts(facts, extra_definitions=siblings)
        call = [f for f in out if f.kind == "calls"][0]
        assert call.target_symbol_id == "python:app.worker::helper"  # unchanged

    def test_idempotent(self):
        once = resolve_repo_facts(_consumer_facts(), extra_definitions=[PRODUCER_ADD])
        twice = resolve_repo_facts(once, extra_definitions=[PRODUCER_ADD])
        key = lambda fs: sorted((f.kind, f.source_symbol_id, f.target_symbol_id) for f in fs)
        assert key(once) == key(twice)

    def test_non_defines_in_extra_are_ignored(self):
        """Only defines in extra_definitions widen the index; a stray non-defines
        sibling fact is ignored (and never emitted)."""
        stray = Fact("calls", "python:other::x", "python:other::y", "other.py", 1, "python")
        out = resolve_repo_facts(
            _consumer_facts(), extra_definitions=[PRODUCER_ADD, stray]
        )
        assert _call_target(out) == "python:shared_lib.math_utils::add"
        assert not any(f.file_path == "other.py" for f in out)
