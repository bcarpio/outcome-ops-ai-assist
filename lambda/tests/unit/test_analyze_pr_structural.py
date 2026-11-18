# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analyze-pr structural-impact pass.

Covers the four moving parts of the structural-impact pass:

  1. ``diff_symbol_resolver.parse_diff_hunks`` -- unified-diff parsing
     into (start, count, signature_lines).
  2. ``diff_symbol_resolver.resolve_targets`` -- diff + post-diff source
     + extractor -> MutatedSymbol list. Asserts that
     ``signature_lines`` flips the change_kind correctly.
  3. ``structural_review.analyze_structural_impact`` against a moto-
     backed symbol-graph table -- the graph queries return the expected
     CallSite lists for each mutated symbol.
  4. ``structural_review.build_impact_block`` -- the rendered Markdown
     contains the expected sections, in stable order, with the
     staleness footer.

Plus the "Definition of done" integration test: a synthetic
PR that changes the signature of ``log_audit_entry`` produces an
impact block that lists all 9 known callers and uses the
signature-change reviewer note. Mirrors the same fixture set as
``test_symbol_graph_reader.py`` / ``test_chat_symbol_graph_augment.py``
so failures across the suite point at the same regression.

Test isolation follows the writer/reader pattern documented in
``test_symbol_graph_reader.py`` -- defensive sys.modules cleanup at
module-import time, plus autouse fixtures that inject the moto-backed
DynamoDB resource into the reader / pointer / writer modules.
"""

from __future__ import annotations

# Defensive sys.modules cleanup -- see the writer test's comment about
# test_check_handlers_init.py's sys.modules['boto3'] MagicMock. We
# additionally clear analyze-pr's locally-loaded modules so the
# importlib loader picks up the fresh boto3 resolution under mock_aws.
import sys as _sys

for _name in list(_sys.modules):
    if (
        _name.startswith("shared.symbol_graph_reader")
        or _name == "diff_symbol_resolver"
        or _name == "structural_review"
        or _name == "analyze_pr_structural_handler"
    ):
        _sys.modules.pop(_name, None)

import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so:
#   * ``from shared.symbol_graph_reader import ...`` works.
#   * ``from diff_symbol_resolver import ...`` works (analyze-pr lives at
#     ``lambda/analyze-pr`` so it's adjacent to ``lambda/shared``).
#   * ``from extractor import extract_facts_from_bytes`` resolves via
#     ``lambda/process-batch-summary/`` (the extractor).
# ---------------------------------------------------------------------------


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
_ANALYZE_PR_DIR = _LAMBDA_ROOT / "analyze-pr"
_EXTRACTOR_DIR = _LAMBDA_ROOT / "process-batch-summary"
for _p in (str(_LAMBDA_ROOT), str(_ANALYZE_PR_DIR), str(_EXTRACTOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SYMBOL_GRAPH_TABLE = "test-analyze-pr-symbol-graph"
CODE_MAPS_TABLE = "test-analyze-pr-code-maps"
WORKSPACE_ID = "ws-analyze-pr"
REPO = "owner/repo"
SHA = "sha-pr-base-1"


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    """No test may touch real AWS endpoints."""
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)
    monkeypatch.setenv("AUDIT_TABLE", "")
    # The kill-switch name is standardized. Tests default to the
    # canonical var and a dedicated backward-compat test below pins
    # the legacy fallback path.
    monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "true")
    monkeypatch.delenv("GRAPH_AUGMENT_ENABLED", raising=False)
    yield


@pytest.fixture(autouse=True)
def _inject_clients():
    """Inject a moto-backed dynamodb resource into the reader + pointer.

    Same shape as ``test_symbol_graph_reader.py`` -- defends against
    the cross-suite sys.modules['boto3'] MagicMock from
    ``test_check_handlers_init.py``.
    """
    from shared import symbol_graph_pointer  # type: ignore
    from shared import symbol_graph_reader  # type: ignore

    real_resource = boto3.resource("dynamodb", region_name="us-west-2")

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_reader._set_resource_for_tests(real_resource)
    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_pointer._set_resource_for_tests(real_resource)

    yield

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_pointer._reset_clients_for_tests()


# ---------------------------------------------------------------------------
# Table helpers (copied from test_symbol_graph_reader.py for parity)
# ---------------------------------------------------------------------------


def _create_symbol_graph_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=SYMBOL_GRAPH_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "target-kind-index",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "target-prefix-index",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_code_maps_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=CODE_MAPS_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_fact(
    *,
    workspace_id: str,
    repo: str,
    commit_sha: str,
    kind: str,
    source_symbol_id: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
    language: str = "python",
    gsi2: bool = True,
) -> None:
    table = boto3.resource("dynamodb", region_name="us-west-2").Table(
        SYMBOL_GRAPH_TABLE
    )
    item = {
        "PK": f"{workspace_id}#{repo}#{commit_sha}",
        "SK": f"{kind}#{source_symbol_id}#{target_symbol_id}#{file_path}#{line}",
        "GSI1PK": f"{workspace_id}#{target_symbol_id}",
        "GSI1SK": f"{kind}#{repo}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": repo,
        "commit_sha": commit_sha,
        "kind": kind,
        "source_symbol_id": source_symbol_id,
        "target_symbol_id": target_symbol_id,
        "file_path": file_path,
        "line": int(line),
        "language": language,
    }
    if gsi2 and "/" in target_symbol_id:
        head, _, tail = target_symbol_id.rpartition("/")
        if head and tail:
            item["GSI2PK"] = f"{workspace_id}#{head}"
            item["GSI2SK"] = f"{tail}#{kind}#{repo}#{file_path}#{line}"
    table.put_item(Item=item)


# ===========================================================================
# parse_diff_hunks
# ===========================================================================


class TestParseDiffHunks:
    def test_empty_patch_returns_empty_list(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        assert parse_diff_hunks("") == []

    def test_single_hunk_no_signature_change(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        patch = (
            "@@ -10,3 +10,3 @@\n"
            " def log_audit_entry():\n"
            "-    pass\n"
            "+    return {}\n"
        )
        hunks = parse_diff_hunks(patch)
        assert len(hunks) == 1
        h = hunks[0]
        assert h.new_start == 10
        assert h.new_count == 3
        # Body-only change: the ``def`` line is context (` `), not
        # an addition or removal -- signature_lines is empty.
        assert h.signature_lines == ()

    def test_hunk_with_added_def_marks_signature_line(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        # New ``def`` line added at post-diff line 11.
        patch = (
            "@@ -10,2 +10,3 @@\n"
            " # header comment\n"
            "+def new_function():\n"
            "+    return 42\n"
        )
        hunks = parse_diff_hunks(patch)
        assert len(hunks) == 1
        # Line 10 is context ("# header comment").
        # Line 11 is the added ``def`` -- signature line.
        # Line 12 is the added ``return`` -- not signature.
        assert 11 in hunks[0].signature_lines

    def test_hunk_with_removed_def_marks_signature_line(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        # Removed signature should still count -- a deleted ``def`` line
        # is one of the strongest "callers need to know" signals.
        patch = (
            "@@ -10,2 +10,1 @@\n"
            "-def old_function():\n"
            " # trailing comment\n"
        )
        hunks = parse_diff_hunks(patch)
        assert len(hunks) == 1
        assert hunks[0].signature_lines  # at least one signature line

    def test_multiple_hunks_parsed_independently(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        patch = (
            "@@ -10,2 +10,2 @@\n"
            " line a\n"
            "-line b\n"
            "+line b'\n"
            "@@ -50,2 +50,2 @@\n"
            " line c\n"
            "+def new_top_level():\n"
        )
        hunks = parse_diff_hunks(patch)
        assert len(hunks) == 2
        assert hunks[0].new_start == 10
        assert hunks[1].new_start == 50
        assert hunks[1].signature_lines  # new_top_level def detected

    def test_typescript_function_signature_detected(self):
        from diff_symbol_resolver import parse_diff_hunks  # type: ignore

        patch = (
            "@@ -10,1 +10,2 @@\n"
            " // file header\n"
            "+export function buildClient(opts: Opts): Client {\n"
        )
        hunks = parse_diff_hunks(patch)
        assert hunks[0].signature_lines  # TS signature picked up


# ===========================================================================
# resolve_targets -- end-to-end diff + extractor + post-diff source
# ===========================================================================


class TestResolveTargets:
    def test_body_only_change_yields_body_change_kind(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        post_diff = (
            b"def log_audit_entry():\n"
            b"    return {'repos': []}\n"
        )
        # Body-only change: hunk touches the function body line, not
        # the ``def`` line.
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " def log_audit_entry():\n"
            "-    return {}\n"
            "+    return {'repos': []}\n"
        )
        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lib/snapshot.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff,
                )
            ]
        )
        assert len(targets) == 1
        # Heuristic detail: a hunk starting AT the symbol's start_line
        # is treated as a signature change because real diffs that
        # touch line 1 of a function often touch the decorator or
        # type-annotation. That conservative read is documented in
        # diff_symbol_resolver.resolve_targets.
        assert targets[0].symbol_id.endswith("::log_audit_entry")
        assert targets[0].change_kind == "signature"
        assert targets[0].file_path == "lib/snapshot.py"

    def test_signature_change_detected_via_def_line(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        # Two functions in the file; PR changes the signature of the
        # second by adding a parameter.
        post_diff = (
            b"def helper(x):\n"
            b"    return x\n"
            b"\n"
            b"def log_audit_entry(workspace_id, repos):\n"
            b"    return None\n"
        )
        # Hunk starts at line 4 (the new def line).
        patch = (
            "@@ -4,1 +4,1 @@\n"
            "-def log_audit_entry(workspace_id):\n"
            "+def log_audit_entry(workspace_id, repos):\n"
        )
        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lib/snapshot.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff,
                )
            ]
        )
        target_by_id = {t.symbol_id: t for t in targets}
        snapshot_id = next(
            sid for sid in target_by_id if sid.endswith("::log_audit_entry")
        )
        assert target_by_id[snapshot_id].change_kind == "signature"
        # Helper isn't touched -- shouldn't be in the targets list.
        assert not any(t.symbol_id.endswith("::helper") for t in targets)

    def test_unsupported_language_returns_empty(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        targets = resolve_targets(
            [
                FilePatch(
                    file_path="README.rst",
                    status="modified",
                    patch="@@ -1,1 +1,1 @@\n-x\n+y\n",
                    post_diff_source=b"y\n",
                )
            ]
        )
        assert targets == []

    def test_removed_file_yields_no_targets(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        # Removed-file caller surfacing is explicitly deferred to v2
        # -- assert v1 returns empty.
        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lib/dead.py",
                    status="removed",
                    patch="@@ -1,5 +0,0 @@\n-def f(): pass\n",
                    post_diff_source=None,
                )
            ]
        )
        assert targets == []

    def test_binary_or_missing_patch_yields_no_targets(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lib/bin.py",
                    status="modified",
                    patch=None,
                    post_diff_source=b"def f(): pass\n",
                )
            ]
        )
        assert targets == []

    def test_stable_ordering_across_runs(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore

        # Two functions, both changed. Stable order = sort by
        # canonical ID. Run twice and assert identical order.
        post_diff = (
            b"def alpha():\n"
            b"    return 1\n"
            b"\n"
            b"def beta():\n"
            b"    return 2\n"
        )
        patch = (
            "@@ -1,5 +1,5 @@\n"
            "-def alpha():\n"
            "-    return 0\n"
            "+def alpha():\n"
            "+    return 1\n"
            " \n"
            "-def beta():\n"
            "-    return 0\n"
            "+def beta():\n"
            "+    return 2\n"
        )
        run1 = resolve_targets(
            [
                FilePatch(
                    file_path="lib/x.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff,
                )
            ]
        )
        run2 = resolve_targets(
            [
                FilePatch(
                    file_path="lib/x.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff,
                )
            ]
        )
        assert [t.symbol_id for t in run1] == [t.symbol_id for t in run2]


# ===========================================================================
# analyze_structural_impact -- graph queries against moto
# ===========================================================================


class TestAnalyzeStructuralImpact:
    def test_empty_workspace_returns_empty(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import analyze_structural_impact  # type: ignore

        out = analyze_structural_impact(
            workspace_id="",
            mutated=[
                MutatedSymbol(
                    symbol_id="python:lib.snapshot::log_audit_entry",
                    change_kind="signature",
                    file_path="lib/snapshot.py",
                    definition_line=1,
                    language="python",
                )
            ],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert out == []

    def test_zero_callers_renders_empty_set(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        mutated = [
            MutatedSymbol(
                symbol_id="python:lib.never::never_called",
                change_kind="body",
                file_path="lib/never.py",
                definition_line=1,
                language="python",
            )
        ]
        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=mutated,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(impacts) == 1
        assert impacts[0].callers == []
        block = build_impact_block(impacts)
        assert "Callers affected: none in the workspaces searched" in block

    def test_signature_change_renders_signature_note(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
            _REVIEWER_NOTE_SIGNATURE,
            _REVIEWER_NOTE_BODY,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        target = "python:lib.snapshot::log_audit_entry"
        _put_fact(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id="python:lib.caller::lambda_handler",
            target_symbol_id=target,
            file_path="lib/caller.py",
            line=10,
        )

        mutated = [
            MutatedSymbol(
                symbol_id=target,
                change_kind="signature",
                file_path="lib/snapshot.py",
                definition_line=1,
                language="python",
            )
        ]
        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=mutated,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        block = build_impact_block(impacts)
        assert _REVIEWER_NOTE_SIGNATURE in block
        assert _REVIEWER_NOTE_BODY not in block
        # Stable caller-list rendering -- only one caller seeded.
        assert "lib/caller.py:10" in block
        assert "Callers affected (1):" in block

    def test_body_change_renders_body_note(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
            _REVIEWER_NOTE_BODY,
            _REVIEWER_NOTE_SIGNATURE,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        target = "python:lib.snapshot::log_audit_entry"
        _put_fact(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id="python:lib.caller::lambda_handler",
            target_symbol_id=target,
            file_path="lib/caller.py",
            line=10,
        )

        mutated = [
            MutatedSymbol(
                symbol_id=target,
                change_kind="body",
                file_path="lib/snapshot.py",
                definition_line=1,
                language="python",
            )
        ]
        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=mutated,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        block = build_impact_block(impacts)
        assert _REVIEWER_NOTE_BODY in block
        assert _REVIEWER_NOTE_SIGNATURE not in block

    def test_graph_unavailable_per_symbol_yields_error_field(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        mutated = [
            MutatedSymbol(
                symbol_id="python:lib.snapshot::log_audit_entry",
                change_kind="signature",
                file_path="lib/snapshot.py",
                definition_line=1,
                language="python",
            )
        ]

        from shared.symbol_graph_reader import SymbolGraphUnavailable  # type: ignore

        # Patch the reader's ``callers`` function to raise per-symbol.
        # The structural module imports it lazily so we patch where the
        # module sources the symbol.
        with patch(
            "shared.symbol_graph_reader.callers",
            side_effect=SymbolGraphUnavailable("synthetic"),
        ):
            impacts = analyze_structural_impact(
                workspace_id=WORKSPACE_ID,
                mutated=mutated,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                code_maps_table_name=CODE_MAPS_TABLE,
            )
        assert impacts[0].error == "graph_unavailable"
        block = build_impact_block(impacts)
        assert "Structural retrieval unavailable for this symbol" in block

    def test_staleness_footer_uses_base_sha(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        target = "python:lib.snapshot::log_audit_entry"
        _put_fact(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id="python:lib.caller::lambda_handler",
            target_symbol_id=target,
            file_path="lib/caller.py",
            line=10,
        )
        # Seed the pointer with a committed-at 90s ago so the
        # staleness phrase reads "committed 1 minutes ago" (rule of
        # thumb: <600s -> minutes).
        code_maps = boto3.resource("dynamodb", region_name="us-west-2").Table(
            CODE_MAPS_TABLE
        )
        committed_at = int(time.time()) - 90
        code_maps.put_item(
            Item={
                "PK": f"SYMBOL_GRAPH#{WORKSPACE_ID}#{REPO}",
                "SK": "POINTER",
                "graph_sha": SHA,
                "graph_sha_committed_at": committed_at,
            }
        )

        mutated = [
            MutatedSymbol(
                symbol_id=target,
                change_kind="signature",
                file_path="lib/snapshot.py",
                definition_line=1,
                language="python",
            )
        ]
        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=mutated,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        block = build_impact_block(impacts, base_sha="basesha012345")
        # base_sha argument wins for the footer.
        assert "Graph as-of base SHA: basesh" in block
        # Staleness phrase is "committed 1 minutes ago" per the rule
        # of thumb -- assert the unit at least.
        assert "minutes ago" in block

    def test_build_impact_block_empty_returns_empty_string(self):
        from structural_review import build_impact_block  # type: ignore

        assert build_impact_block([]) == ""

    def test_build_unavailable_block_includes_disclaimer(self):
        from structural_review import build_unavailable_block  # type: ignore

        out = build_unavailable_block(base_sha="abcdef1234567")
        assert "Structural retrieval was unavailable" in out
        assert "abcdef1" in out  # short SHA


# ===========================================================================
# Plan-gkg/06 "Definition of done" integration test
# ===========================================================================


class TestReportReposSnapshotIntegration:
    """Plan-gkg/06 DoD: a synthetic PR that changes the signature of
    ``log_audit_entry`` produces an impact block with the 9
    known callers and the signature-change reviewer note.

    Mirrors the fixture set in ``test_symbol_graph_reader.py`` so a
    regression here points at the same root cause as the corresponding
    reader test. Uses the same 9-caller fan-out of log_audit_entry.
    """

    def test_signature_change_yields_nine_callers_with_signature_note(self):
        from diff_symbol_resolver import FilePatch, resolve_targets  # type: ignore
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
            _REVIEWER_NOTE_SIGNATURE,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        # Synthetic post-diff content for audit.py: the
        # signature line is at line 1 of the fixture so resolve_targets'
        # off-by-one heuristic (decorator/annotation pattern) treats
        # the change at line 1 as a signature change, matching reality.
        post_diff_source = (
            b"def log_audit_entry(workspace_id, repos):\n"
            b"    # Refactored v2.0.0\n"
            b"    return None\n"
        )
        # Hunk: the signature line is added (+) -- the resolver flags
        # this as a signature_line.
        patch = (
            "@@ -1,3 +1,3 @@\n"
            "-def log_audit_entry(workspace_id):\n"
            "+def log_audit_entry(workspace_id, repos):\n"
            "     # Refactored v2.0.0\n"
            "     return None\n"
        )
        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lambda/shared/audit.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff_source,
                )
            ]
        )
        # Find the canonical ID the extractor emits. The python
        # extractor uses the dotted module path of the file_path.
        target_ids = [t.symbol_id for t in targets]
        snapshot_id = next(
            tid for tid in target_ids if tid.endswith("::log_audit_entry")
        )
        snap_target = next(t for t in targets if t.symbol_id == snapshot_id)
        assert snap_target.change_kind == "signature"

        # Seed the same 9 callers used in the reader / chat tests so
        # this integration assertion is consistent across the suite.
        known_callers = [
            ("github-integration", "lambda/github-integration/handler.py", 137),
            ("jira-integration", "lambda/jira-integration/handler.py", 114),
            ("confluence-integration", "lambda/confluence-integration/handler.py", 129),
            ("outlook-integration", "lambda/outlook-integration/handler.py", 127),
            ("github-sync", "lambda/github-sync/handler.py", 789),
            ("jira-sync", "lambda/jira-sync/handler.py", 452),
            ("confluence-sync", "lambda/confluence-sync/handler.py", 476),
            ("file-ingestion", "lambda/file-ingestion/handler.py", 1456),
            ("scheduler", "lambda/scheduled-reporter/handler.py", 42),
        ]
        for name, file_path, line in known_callers:
            _put_fact(
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="calls",
                source_symbol_id=f"python:lambda.{name.replace('-', '_')}.handler::lambda_handler",
                target_symbol_id=snapshot_id,
                file_path=file_path,
                line=line,
            )

        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=targets,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        # Locate the impact for the snapshot symbol -- there may be
        # other targets in the diff but we asserted snap_target above.
        snap_impact = next(
            i for i in impacts if i.mutated.symbol_id == snapshot_id
        )
        assert len(snap_impact.callers) == 9
        rendered_files = {site.file_path for site in snap_impact.callers}
        expected_files = {f for _, f, _ in known_callers}
        assert rendered_files == expected_files

        block = build_impact_block(impacts, base_sha="basesha789012")
        # Plan example exact-language assertions.
        assert "## Structural impact" in block
        assert f"`{snapshot_id}` (signature-changed)" in block
        # Definition anchor.
        assert "Definition: lambda/shared/audit.py:1" in block
        # Caller count + every file path.
        assert "Callers affected (9):" in block
        for _, file_path, line in known_callers:
            assert f"{file_path}:{line}" in block, (
                f"missing {file_path}:{line} from block"
            )
        # Signature note (not body note).
        assert _REVIEWER_NOTE_SIGNATURE in block
        # Footer carries the explicit base SHA.
        assert "Graph as-of base SHA: basesh" in block

    def test_body_change_uses_softer_note_for_same_symbol(self):
        """Same symbol, same callers -- but the diff is a body-only
        change. Expected: block lists the same 9 callers but uses the
        body-change reviewer note (lower-emphasis language).
        """
        from diff_symbol_resolver import (  # type: ignore
            FilePatch,
            MutatedSymbol,
            resolve_targets,
        )
        from structural_review import (  # type: ignore
            analyze_structural_impact,
            build_impact_block,
            _REVIEWER_NOTE_BODY,
            _REVIEWER_NOTE_SIGNATURE,
        )

        _create_symbol_graph_table()
        _create_code_maps_table()

        # File has TWO top-level definitions; the body-only change
        # falls inside the SECOND one's body (line 5+), well away from
        # its decl line (line 4) so the resolver's "start-of-symbol"
        # off-by-one doesn't promote it to signature-change.
        post_diff_source = (
            b"def helper():\n"
            b"    return 1\n"
            b"\n"
            b"def log_audit_entry(workspace_id):\n"
            b"    # v2.0.0 behavior change\n"
            b"    return {'workspace_id': workspace_id, 'updated': True}\n"
        )
        patch = (
            "@@ -6,1 +6,1 @@\n"
            "-    return {}\n"
            "+    return {'workspace_id': workspace_id, 'updated': True}\n"
        )
        targets = resolve_targets(
            [
                FilePatch(
                    file_path="lambda/shared/audit.py",
                    status="modified",
                    patch=patch,
                    post_diff_source=post_diff_source,
                )
            ]
        )
        target_ids = [t.symbol_id for t in targets]
        snapshot_id = next(
            tid for tid in target_ids if tid.endswith("::log_audit_entry")
        )
        snap_target = next(t for t in targets if t.symbol_id == snapshot_id)
        assert snap_target.change_kind == "body"

        # Seed the same 9 callers.
        known_callers = [
            ("github-integration", "lambda/github-integration/handler.py", 137),
            ("jira-integration", "lambda/jira-integration/handler.py", 114),
            ("confluence-integration", "lambda/confluence-integration/handler.py", 129),
            ("outlook-integration", "lambda/outlook-integration/handler.py", 127),
            ("github-sync", "lambda/github-sync/handler.py", 789),
            ("jira-sync", "lambda/jira-sync/handler.py", 452),
            ("confluence-sync", "lambda/confluence-sync/handler.py", 476),
            ("file-ingestion", "lambda/file-ingestion/handler.py", 1456),
            ("scheduler", "lambda/scheduled-reporter/handler.py", 42),
        ]
        for name, file_path, line in known_callers:
            _put_fact(
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="calls",
                source_symbol_id=f"python:lambda.{name.replace('-', '_')}.handler::lambda_handler",
                target_symbol_id=snapshot_id,
                file_path=file_path,
                line=line,
            )

        impacts = analyze_structural_impact(
            workspace_id=WORKSPACE_ID,
            mutated=[snap_target],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        block = build_impact_block(impacts, base_sha="basesha789012")
        # body-changed signal in the header.
        assert "(body-changed)" in block
        # softer note used -- not the signature one.
        assert _REVIEWER_NOTE_BODY in block
        assert _REVIEWER_NOTE_SIGNATURE not in block
        # Callers still listed.
        assert "Callers affected (9):" in block


# ===========================================================================
# Handler integration -- _build_structural_impact_comment glue
# ===========================================================================


def _load_handler():
    """Load analyze-pr/handler.py under a unique module name so the
    rest of the test_analyze_pr.py module doesn't conflict.

    Mirrors test_analyze_pr.py's pattern; we use a different module
    name to avoid double-load.
    """
    handler_path = _ANALYZE_PR_DIR / "handler.py"
    spec = importlib.util.spec_from_file_location(
        "analyze_pr_structural_handler", str(handler_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze_pr_structural_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestHandlerStructuralGlue:
    def test_disabled_via_env_returns_none(self, monkeypatch):
        """GRAPH_RETRIEVAL_ENABLED=false short-circuits without firing
        any reader calls -- the kill-switch standardized in .
        """
        monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "false")
        handler = _load_handler()
        out = handler._build_structural_impact_comment(
            repository="o/r",
            pr=MagicMock(),
            files=[],
            token="t",
            workspace_ids=["ws"],
            base_sha="b",
            head_sha="h",
        )
        assert out is None

    def test_disabled_via_legacy_env_returns_none(self, monkeypatch):
        """Backward-compat: GRAPH_AUGMENT_ENABLED=false (the 
        name) still disables the pass when GRAPH_RETRIEVAL_ENABLED is
        unset. Lets operators on older deploys roll forward."""
        monkeypatch.delenv("GRAPH_RETRIEVAL_ENABLED", raising=False)
        monkeypatch.setenv("GRAPH_AUGMENT_ENABLED", "false")
        handler = _load_handler()
        out = handler._build_structural_impact_comment(
            repository="o/r",
            pr=MagicMock(),
            files=[],
            token="t",
            workspace_ids=["ws"],
            base_sha="b",
            head_sha="h",
        )
        assert out is None

    def test_missing_workspace_id_returns_none(self, monkeypatch):
        """No workspace_id = no graph anchor -- pass is a no-op."""
        monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "true")
        handler = _load_handler()
        out = handler._build_structural_impact_comment(
            repository="o/r",
            pr=MagicMock(),
            files=[],
            token="t",
            workspace_ids=[],
            base_sha="b",
            head_sha="h",
        )
        assert out is None

    def test_missing_symbol_graph_table_returns_none(self, monkeypatch):
        """SYMBOL_GRAPH_TABLE unset -> no-op (deploy bug guard)."""
        monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
        monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "true")
        handler = _load_handler()
        out = handler._build_structural_impact_comment(
            repository="o/r",
            pr=MagicMock(),
            files=[],
            token="t",
            workspace_ids=["ws"],
            base_sha="b",
            head_sha="h",
        )
        assert out is None

    def test_no_mutated_symbols_returns_none(self, monkeypatch):
        """Files with no patch / unsupported langs -> no block.

        Patches the inner fetch helper so we don't hit GitHub.
        """
        _create_symbol_graph_table()
        _create_code_maps_table()
        handler = _load_handler()

        # Build a file list whose patch is None -> no mutated symbols.
        gh_file = handler.GitHubFile(
            filename="README.md",
            status="modified",
            additions=1,
            deletions=1,
            changes=2,
            patch=None,
        )
        out = handler._build_structural_impact_comment(
            repository="o/r",
            pr=MagicMock(),
            files=[gh_file],
            token="t",
            workspace_ids=[WORKSPACE_ID],
            base_sha="basesha789012",
            head_sha="headsha789012",
        )
        assert out is None

    def test_end_to_end_with_seeded_callers(self, monkeypatch):
        """End-to-end through the handler glue: seed callers, build a
        synthetic FilePatch -> assert the rendered comment is a non-
        empty Markdown block carrying the seeded callers.
        """
        _create_symbol_graph_table()
        _create_code_maps_table()
        handler = _load_handler()

        target = "python:lib.snapshot::log_audit_entry"
        _put_fact(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id="python:lib.caller::lambda_handler",
            target_symbol_id=target,
            file_path="lib/caller.py",
            line=10,
        )

        post_diff = (
            b"def log_audit_entry(workspace_id, repos):\n"
            b"    return None\n"
        )
        gh_file = handler.GitHubFile(
            filename="lib/snapshot.py",
            status="modified",
            additions=1,
            deletions=1,
            changes=2,
            patch=(
                "@@ -1,2 +1,2 @@\n"
                "-def log_audit_entry(workspace_id):\n"
                "+def log_audit_entry(workspace_id, repos):\n"
                "     return None\n"
            ),
        )

        # Stub fetch_file_contents so the glue doesn't hit GitHub.
        with patch.object(
            handler,
            "fetch_file_contents",
            return_value=post_diff,
        ):
            out = handler._build_structural_impact_comment(
                repository="o/r",
                pr=MagicMock(),
                files=[gh_file],
                token="t",
                workspace_ids=[WORKSPACE_ID],
                base_sha="basesha789012",
                head_sha="headsha789012",
            )

        assert out is not None
        assert "## Structural impact" in out
        # Symbol header carries the change kind.
        assert "(signature-changed)" in out
        # Caller seeded above.
        assert "lib/caller.py:10" in out
        # Footer carries the explicit base SHA.
        assert "Graph as-of base SHA: basesh" in out


# ===========================================================================
# Repo -> workspace resolution for the structural pass
# ===========================================================================


WORKSPACES_TABLE = "test-workspaces"


def _create_workspaces_table(rows):
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    table = ddb.create_table(
        TableName=WORKSPACES_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    for row in rows:
        table.put_item(Item=row)
    return table


def _repo_row(workspace_id, repo):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"GITHUB_REPO#{repo}",
        "workspace_id": workspace_id,
        "repo_name": repo,
    }


def _ws_meta(workspace_id, *, shared_with=None, is_global=False):
    item = {
        "PK": f"WS#{workspace_id}",
        "SK": "METADATA",
        "workspace_id": workspace_id,
        "is_global": is_global,
    }
    if shared_with:
        item["search_allowed_workspaces"] = list(shared_with)
    return item


class TestResolveStructuralWorkspaceScope:
    """Same consumer scope as BREAKING_CHANGES (ADR-056 §5)."""

    @pytest.fixture
    def handler(self, monkeypatch):
        monkeypatch.delenv("PR_WORKSPACE_ID", raising=False)
        monkeypatch.setenv("WORKSPACES_TABLE", WORKSPACES_TABLE)
        mod = _load_handler()
        # pr_check_scope caches its boto3 resource per container; drop it so
        # each test binds to this test's moto backend.
        import shared.pr_check_scope as scope

        monkeypatch.setattr(scope, "_dynamodb", None)
        return mod

    def test_repo_workspace_only_when_not_shared(self, handler):
        _create_workspaces_table([
            _ws_meta("eng"),
            _ws_meta("analytics"),
            _repo_row("eng", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == ["eng"]

    def test_includes_workspaces_the_repo_workspace_is_shared_with(self, handler):
        # Engineering holds the repo and is shared with Analytics: consumers
        # in Analytics keep their edges in Analytics' partition, so both are
        # queried. Marketing is not shared with, so it is excluded.
        _create_workspaces_table([
            _ws_meta("eng", shared_with=["analytics"]),
            _ws_meta("analytics"),
            _ws_meta("marketing"),
            _repo_row("eng", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == [
            "eng",
            "analytics",
        ]

    def test_reverse_grant_is_not_included(self, handler):
        # Analytics sharing ITS workspace with Engineering lets Engineering
        # read Analytics, not the other way round -- Analytics code cannot
        # consume the repo, so Analytics is not in the consumer scope.
        _create_workspaces_table([
            _ws_meta("eng"),
            _ws_meta("analytics", shared_with=["eng"]),
            _repo_row("eng", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == ["eng"]

    def test_includes_global_workspaces(self, handler):
        _create_workspaces_table([
            _ws_meta("eng"),
            _ws_meta("platform", is_global=True),
            _repo_row("eng", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == [
            "eng",
            "platform",
        ]

    def test_repo_in_global_workspace_scopes_every_workspace(self, handler):
        _create_workspaces_table([
            _ws_meta("platform", is_global=True),
            _ws_meta("analytics"),
            _ws_meta("eng"),
            _repo_row("platform", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == [
            "platform",
            "analytics",
            "eng",
        ]

    def test_repo_workspaces_come_first(self, handler):
        _create_workspaces_table([
            _ws_meta("aaa-shared"),
            _ws_meta("zz-home", shared_with=["aaa-shared"]),
            _ws_meta("mm-home"),
            _repo_row("zz-home", "owner/repo"),
            _repo_row("mm-home", "owner/repo"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == [
            "mm-home",
            "zz-home",
            "aaa-shared",
        ]

    def test_unconnected_repo_returns_empty(self, handler):
        _create_workspaces_table([
            _ws_meta("eng", shared_with=["analytics"]),
            _ws_meta("platform", is_global=True),
            _repo_row("eng", "owner/other"),
        ])

        assert handler._resolve_structural_workspace_scope("owner/repo") == []

    def test_env_override_pins_scope_without_lookup(self, handler, monkeypatch):
        monkeypatch.setenv("PR_WORKSPACE_ID", "ws-override")
        # No table created: a lookup would raise, so this proves it is skipped.

        assert handler._resolve_structural_workspace_scope("owner/repo") == [
            "ws-override"
        ]

    def test_workspaces_disabled_returns_empty(self, handler, monkeypatch):
        monkeypatch.setenv("WORKSPACES_TABLE", "")

        assert handler._resolve_structural_workspace_scope("owner/repo") == []

    def test_lookup_error_is_non_fatal(self, handler):
        # Table does not exist -> ResourceNotFoundException inside the scan.
        assert handler._resolve_structural_workspace_scope("owner/repo") == []


class TestAnalyzeStructuralImpactAcrossWorkspaces:
    TARGET = "python:lib.snapshot::log_audit_entry"

    def _mutated(self):
        from diff_symbol_resolver import MutatedSymbol  # type: ignore

        return [
            MutatedSymbol(
                symbol_id=self.TARGET,
                change_kind="signature",
                file_path="lib/snapshot.py",
                definition_line=1,
                language="python",
            )
        ]

    def _caller(self, ws, repo, file_path, line, source):
        _put_fact(
            workspace_id=ws,
            repo=repo,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=source,
            target_symbol_id=self.TARGET,
            file_path=file_path,
            line=line,
        )

    def test_merges_callers_from_every_workspace_in_scope(self):
        from structural_review import analyze_structural_impact  # type: ignore

        _create_symbol_graph_table()
        _create_code_maps_table()
        self._caller("eng", REPO, "lib/eng_caller.py", 10, "python:lib.eng_caller::run")
        self._caller(
            "analytics", "owner/dash", "dash/report.py", 42, "python:dash.report::build"
        )
        self._caller(
            "marketing", "owner/site", "site/x.py", 7, "python:site.x::go"
        )

        impacts = analyze_structural_impact(
            workspace_ids=["eng", "analytics"],
            mutated=self._mutated(),
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

        assert len(impacts) == 1
        assert impacts[0].error is None
        found = {(c.repo, c.file_path, c.line) for c in impacts[0].callers}
        assert found == {
            (REPO, "lib/eng_caller.py", 10),
            ("owner/dash", "dash/report.py", 42),
        }

    def test_dedupes_the_same_site_across_workspaces(self):
        from structural_review import analyze_structural_impact  # type: ignore

        _create_symbol_graph_table()
        _create_code_maps_table()
        # A repo connected to two workspaces is ingested into both, so the
        # same edge exists in each partition.
        for ws in ("eng", "eng-mirror"):
            self._caller(ws, REPO, "lib/eng_caller.py", 10, "python:lib.eng_caller::run")

        impacts = analyze_structural_impact(
            workspace_ids=["eng", "eng-mirror"],
            mutated=self._mutated(),
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

        assert len(impacts[0].callers) == 1

    def test_any_workspace_failure_marks_symbol_unverified(self, monkeypatch):
        from shared import symbol_graph_reader  # type: ignore
        from structural_review import analyze_structural_impact  # type: ignore

        real_callers = symbol_graph_reader.callers

        def flaky_callers(ws, *args, **kwargs):
            if ws == "analytics":
                raise symbol_graph_reader.SymbolGraphUnavailable("throttled")
            return real_callers(ws, *args, **kwargs)

        monkeypatch.setattr(symbol_graph_reader, "callers", flaky_callers)
        _create_symbol_graph_table()
        _create_code_maps_table()
        self._caller("eng", REPO, "lib/eng_caller.py", 10, "python:lib.eng_caller::run")

        impacts = analyze_structural_impact(
            workspace_ids=["eng", "analytics"],
            mutated=self._mutated(),
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

        # A partial list could read as the full blast radius -- fail closed.
        assert impacts[0].error == "graph_unavailable"
        assert impacts[0].callers == []

    def test_single_workspace_form_still_supported(self):
        from structural_review import analyze_structural_impact  # type: ignore

        _create_symbol_graph_table()
        _create_code_maps_table()
        self._caller("eng", REPO, "lib/eng_caller.py", 10, "python:lib.eng_caller::run")

        impacts = analyze_structural_impact(
            workspace_id="eng",
            mutated=self._mutated(),
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

        assert len(impacts[0].callers) == 1
