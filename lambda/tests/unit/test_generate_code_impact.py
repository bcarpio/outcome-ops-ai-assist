# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``generate_code.impact_analysis``.

Covers:

- ``classify_task_type``: modify / greenfield / ambiguous (defaults to modify)
- ``extract_modify_targets``: Python ``module::name`` IDs + TS subpath IDs,
  signature-change heuristic, dedup
- ``analyze_caller_impact``: graph integration via the symbol-graph
  reader; graceful degradation when the graph is unavailable or
  workspace_id is missing
- ``build_impact_prompt_section``: format parity with the documented
  example
- Definition-of-done: a synthetic modify-task changing the signature
  of ``log_audit_entry`` produces a plan that lists all 9 caller
  updates
- Greenfield-path no-op: no graph queries fire, no impact section
  produced

Test isolation mirrors ``test_symbol_graph_reader.py`` —
``mock_aws`` autouse + ``_set_resource_for_tests`` injection on the
reader/pointer/writer modules — because this test exercises the same
DynamoDB-backed reader. We additionally defend against the
``test_check_handlers_init.py`` boto3 MagicMock contamination by
clearing any cached ``shared.symbol_graph_*`` modules at module load.
"""

from __future__ import annotations

# Cross-suite defense: clear any cached symbol_graph modules so the
# autouse fixture below resolves the real boto3 at import time. See
# ``test_check_handlers_init.py:16`` for the MagicMock that this
# defense protects against. The same pattern is used by
# ``test_symbol_graph_reader.py`` and ``test_symbol_graph_writer.py``.
import sys as _sys
for _name in list(_sys.modules):
    if _name.startswith("shared.symbol_graph_") or _name == "impact_analysis":
        _sys.modules.pop(_name, None)

import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from impact_analysis import ...`` and
# ``from shared.symbol_graph_reader import ...`` both resolve. The
# generate-code dir holds impact_analysis; the lambda root holds shared.
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_LAMBDA_ROOT = _TESTS_DIR.parents[1]
_GENERATE_CODE_DIR = _LAMBDA_ROOT / "generate-code"

for _p in (_LAMBDA_ROOT, _GENERATE_CODE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL_GRAPH_TABLE = "test-impact-symbol-graph"
CODE_MAPS_TABLE = "test-impact-code-maps"
WORKSPACE_ID = "ws-impact"

# 9-caller fixture mirrors the real license-layer v2.0.0 migration —
# the chunk-07 Definition of Done's named example.
REPORT_REPOS_SYMBOL = (
    "python:lambda.shared.audit::log_audit_entry"
)
REPORT_REPOS_CALLERS = [
    (
        "python:lambda.github_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/github-integration/handler.py",
        137,
    ),
    (
        "python:lambda.confluence_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/confluence-integration/handler.py",
        84,
    ),
    (
        "python:lambda.database_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/database-integration/handler.py",
        91,
    ),
    (
        "python:lambda.jira_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/jira-integration/handler.py",
        72,
    ),
    (
        "python:lambda.servicenow_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/servicenow-integration/handler.py",
        88,
    ),
    (
        "python:lambda.salesforce_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/salesforce-integration/handler.py",
        90,
    ),
    (
        "python:lambda.slack_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/slack-integration/handler.py",
        77,
    ),
    (
        "python:lambda.gitlab_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/gitlab-integration/handler.py",
        81,
    ),
    (
        "python:lambda.sharepoint_integration.handler::lambda_handler",
        "owner/repo",
        "lambda/sharepoint-integration/handler.py",
        85,
    ),
]
assert len(REPORT_REPOS_CALLERS) == 9, "Fixture lost its 9 callers"


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
def _reset_shared_clients():
    """Inject this file's real boto3.resource into every shared graph
    module that has the _set_resource_for_tests hook. Mirrors the
    pattern in test_symbol_graph_reader.py — keeps the reader resolving
    against moto rather than the MagicMock that test_check_handlers_init
    may have planted in sys.modules['boto3'].
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


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
    monkeypatch.delenv("CODE_MAPS_TABLE", raising=False)
    monkeypatch.delenv("WORKSPACE_ID", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


# ---------------------------------------------------------------------------
# Table fixtures
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


@pytest.fixture
def tables():
    _create_symbol_graph_table()
    _create_code_maps_table()
    return boto3.resource("dynamodb", region_name="us-west-2")


def _put_fact(
    table,
    *,
    workspace_id: str,
    repo: str,
    commit_sha: str,
    kind: str,
    source_symbol_id: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
    language: str,
    gsi2: bool = True,
) -> None:
    """Write a Fact row matching the chunk-02 writer's key layout."""
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


def _seed_log_audit_entry_callers(tables_resource):
    """Seed the 9 known callers from the license-layer v2.0.0 migration."""
    table = tables_resource.Table(SYMBOL_GRAPH_TABLE)
    for source_id, repo, file_path, line in REPORT_REPOS_CALLERS:
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=repo,
            commit_sha="sha-fixture",
            kind="calls",
            source_symbol_id=source_id,
            target_symbol_id=REPORT_REPOS_SYMBOL,
            file_path=file_path,
            line=line,
            language="python",
        )


# ===========================================================================
# classify_task_type
# ===========================================================================


class TestClassifyTaskType:
    def test_modify_verb_in_title(self):
        from impact_analysis import classify_task_type  # type: ignore

        c = classify_task_type(
            issue_title="Rename log_audit_entry to capture metadata",
            issue_description=None,
        )
        assert c.task_type == "modify"
        assert "modification" in c.reason

    def test_greenfield_verb_only(self):
        from impact_analysis import classify_task_type  # type: ignore

        c = classify_task_type(
            issue_title="Add a new analytics aggregator",
            issue_description="Create a Lambda that summarizes events.",
        )
        assert c.task_type == "greenfield"

    def test_ambiguous_defaults_to_modify(self):
        from impact_analysis import classify_task_type  # type: ignore

        c = classify_task_type(
            issue_title="Investigate analytics",
            issue_description="Some thoughts about analytics.",
        )
        assert c.task_type == "modify"
        assert "ambiguous" in c.reason

    def test_explicit_files_to_modify_forces_modify(self):
        from impact_analysis import classify_task_type  # type: ignore

        # Even with greenfield-only verbiage, an explicit modify list
        # overrides — the plan is touching existing files.
        c = classify_task_type(
            issue_title="Add new analytics",
            issue_description="",
            files_to_create=[],
            files_to_modify=["lambda/foo/handler.py"],
        )
        assert c.task_type == "modify"
        assert "1 existing files" in c.reason

    def test_mixed_modify_wins(self):
        """When the issue has both modify and greenfield verbs, modify wins
        (conservative default + impact analysis is additive)."""
        from impact_analysis import classify_task_type  # type: ignore

        c = classify_task_type(
            issue_title="Refactor to add a new endpoint",
            issue_description=None,
        )
        assert c.task_type == "modify"


# ===========================================================================
# extract_modify_targets
# ===========================================================================


class TestExtractModifyTargets:
    def test_python_symbol_extracted(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = (
            "Step 1: Rename "
            "python:lambda.shared.audit::log_audit_entry "
            "and update the parameter list."
        )
        targets = extract_modify_targets(plan)
        assert len(targets) == 1
        assert (
            targets[0].symbol_id
            == "python:lambda.shared.audit::log_audit_entry"
        )
        assert targets[0].signature_change is True  # 'parameter' keyword

    def test_python_symbol_without_prefix_defaults_to_python(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = "Update lambda.shared.audit::log_audit_entry to v2."
        targets = extract_modify_targets(plan)
        assert len(targets) == 1
        assert targets[0].symbol_id.startswith("python:")

    def test_typescript_subpath_extracted(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = "Migrate consumers off @acme-corp/shared-utils/http2 to /http3."
        targets = extract_modify_targets(plan)
        sids = [t.symbol_id for t in targets]
        assert "typescript:@acme-corp/shared-utils/http2" in sids

    def test_dedup_collapses_duplicate_mentions(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = (
            "foo.bar::baz once. foo.bar::baz twice. foo.bar::baz thrice."
        )
        targets = extract_modify_targets(plan)
        assert len(targets) == 1
        assert targets[0].raw_mentions == 3

    def test_no_symbols_returns_empty(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        targets = extract_modify_targets("This plan has no symbol IDs at all.")
        assert targets == []

    def test_signature_keyword_anywhere_in_window(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = (
            "We need to change the signature of foo.bar::baz so that we can "
            "accept the new metadata argument."
        )
        targets = extract_modify_targets(plan)
        assert len(targets) == 1
        assert targets[0].signature_change is True

    def test_body_only_change_when_no_signature_keyword(self):
        from impact_analysis import extract_modify_targets  # type: ignore

        plan = "Fix a logging bug inside foo.bar::baz — output was misformatted."
        targets = extract_modify_targets(plan)
        assert len(targets) == 1
        assert targets[0].signature_change is False

    def test_extracts_from_issue_description_too(self):
        """Real issues sometimes name the symbol in the body and the plan
        only describes the work prose-style. Issue body must be searched."""
        from impact_analysis import extract_modify_targets  # type: ignore

        targets = extract_modify_targets(
            plan_text="Step 1: Make the change described in the issue.",
            issue_title="Update log_audit_entry",
            issue_description=(
                "Rename python:lambda.shared.audit::log_audit_entry."
            ),
        )
        sids = [t.symbol_id for t in targets]
        assert (
            "python:lambda.shared.audit::log_audit_entry"
            in sids
        )


# ===========================================================================
# analyze_caller_impact
# ===========================================================================


class TestAnalyzeCallerImpact:
    def test_no_workspace_id_returns_empty(self):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            analyze_caller_impact,
        )

        # No workspace -> no graph queries possible. Returning [] is the
        # documented graceful-degradation path.
        result = analyze_caller_impact(
            workspace_id="",
            targets=[
                ModifyTarget(
                    symbol_id="python:foo::bar",
                    signature_change=False,
                    raw_mentions=1,
                )
            ],
        )
        assert result == []

    def test_callers_query_routes_for_function_symbols(self, tables):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            analyze_caller_impact,
        )

        _seed_log_audit_entry_callers(tables)
        target = ModifyTarget(
            symbol_id=REPORT_REPOS_SYMBOL,
            signature_change=True,
            raw_mentions=1,
        )
        impacts = analyze_caller_impact(
            workspace_id=WORKSPACE_ID,
            targets=[target],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(impacts) == 1
        assert impacts[0].error is None
        assert len(impacts[0].callers) == 9

    def test_importers_query_routes_for_subpath_symbols(self, tables):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            analyze_caller_impact,
        )

        table = tables.Table(SYMBOL_GRAPH_TABLE)
        # Seed two importers of @acme-corp/shared-utils/http2. The reader
        # treats the language-prefixed target as the GSI1PK partition,
        # so the writer-stored target_symbol_id must carry the same
        # prefix (mirrors test_symbol_graph_reader.py seeding).
        for i in range(2):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=f"owner/app-{i}",
                commit_sha="sha",
                kind="imports",
                source_symbol_id=f"ts:apps/app-{i}::index",
                target_symbol_id="typescript:@acme-corp/shared-utils/http2",
                file_path=f"apps/app-{i}/index.ts",
                line=4,
                language="typescript",
            )
        target = ModifyTarget(
            symbol_id="typescript:@acme-corp/shared-utils/http2",
            signature_change=False,
            raw_mentions=1,
        )
        impacts = analyze_caller_impact(
            workspace_id=WORKSPACE_ID,
            targets=[target],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(impacts) == 1
        # The reader returns exact-subpath consumers under GSI1.
        assert impacts[0].error is None
        assert len(impacts[0].callers) == 2

    def test_graph_unavailable_records_error_but_continues(self, tables):
        """One target throws SymbolGraphUnavailable; the other returns
        normally. The result contains both, with error set on the
        failed one."""
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            analyze_caller_impact,
        )

        _seed_log_audit_entry_callers(tables)

        targets = [
            ModifyTarget(
                symbol_id="python:never.exists::sym",
                signature_change=False,
                raw_mentions=1,
            ),
            ModifyTarget(
                symbol_id=REPORT_REPOS_SYMBOL,
                signature_change=True,
                raw_mentions=1,
            ),
        ]

        # Patch the reader's callers() so the FIRST target raises and the
        # SECOND falls through to the real implementation. Easiest way is
        # to wrap the original and selectively raise.
        import shared.symbol_graph_reader as reader  # type: ignore

        original_callers = reader.callers

        def selective_callers(workspace_id, target_symbol_id, **kw):
            if target_symbol_id == "python:never.exists::sym":
                raise reader.SymbolGraphUnavailable("simulated transient")
            return original_callers(workspace_id, target_symbol_id, **kw)

        with patch.object(reader, "callers", side_effect=selective_callers):
            impacts = analyze_caller_impact(
                workspace_id=WORKSPACE_ID,
                targets=targets,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                code_maps_table_name=CODE_MAPS_TABLE,
            )

        assert len(impacts) == 2
        # By target ordering (input order is preserved by the impl).
        first, second = impacts
        assert first.error == "graph_unavailable"
        assert first.callers == []
        assert second.error is None
        assert len(second.callers) == 9


# ===========================================================================
# build_impact_prompt_section
# ===========================================================================


class TestBuildImpactPromptSection:
    def test_empty_impacts_returns_empty_string(self):
        from impact_analysis import build_impact_prompt_section  # type: ignore

        assert build_impact_prompt_section([]) == ""

    def test_signature_change_lists_callers(self, tables):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            SymbolImpact,
            build_impact_prompt_section,
        )
        from shared.symbol_graph_reader import CallSite  # type: ignore

        impact = SymbolImpact(
            target=ModifyTarget(
                symbol_id=REPORT_REPOS_SYMBOL,
                signature_change=True,
                raw_mentions=2,
            ),
            callers=[
                CallSite(
                    file_path="lambda/github-integration/handler.py",
                    line=137,
                    repo="owner/repo",
                    source_symbol_id="caller-1",
                    language="python",
                ),
                CallSite(
                    file_path="lambda/confluence-integration/handler.py",
                    line=84,
                    repo="owner/repo",
                    source_symbol_id="caller-2",
                    language="python",
                ),
            ],
            truncated=False,
            error=None,
        )
        section = build_impact_prompt_section([impact])
        assert "your proposed changes will modify the following symbols" in section
        assert REPORT_REPOS_SYMBOL in section
        assert "signature change planned" in section
        assert "2 callers will need invocation updates" in section
        assert "lambda/github-integration/handler.py:137" in section
        assert "lambda/confluence-integration/handler.py:84" in section
        assert "do not silently ship a breaking change" in section.lower()

    def test_truncation_is_noted(self, tables):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            SymbolImpact,
            build_impact_prompt_section,
        )
        from shared.symbol_graph_reader import CallSite  # type: ignore

        impact = SymbolImpact(
            target=ModifyTarget(
                symbol_id="python:foo::bar",
                signature_change=True,
                raw_mentions=1,
            ),
            callers=[
                CallSite(
                    file_path="caller.py",
                    line=1,
                    repo="owner/repo",
                    source_symbol_id="x",
                    language="python",
                )
            ],
            truncated=True,
            error=None,
        )
        section = build_impact_prompt_section([impact])
        assert "truncated" in section.lower()

    def test_graph_unavailable_renders_skip_line(self):
        from impact_analysis import (  # type: ignore
            ModifyTarget,
            SymbolImpact,
            build_impact_prompt_section,
        )

        impact = SymbolImpact(
            target=ModifyTarget(
                symbol_id="python:foo::bar",
                signature_change=True,
                raw_mentions=1,
            ),
            callers=[],
            truncated=False,
            error="graph_unavailable",
        )
        section = build_impact_prompt_section([impact])
        assert "symbol graph unavailable" in section


# ===========================================================================
# End-to-end orchestrator: run_impact_analysis
# ===========================================================================


class TestRunImpactAnalysis:
    def test_greenfield_path_skips_graph(self, tables):
        """Greenfield issue -> skipped=True, prompt_section='', no DB hit."""
        from impact_analysis import run_impact_analysis  # type: ignore

        # No facts seeded — and we assert that doesn't matter (the
        # greenfield path should never reach the graph).
        result = run_impact_analysis(
            issue_title="Add a new analytics aggregator",
            issue_description="Create the new Lambda.",
            plan_text="Step 1: Create lambda/analytics/handler.py.",
            workspace_id=WORKSPACE_ID,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert result.skipped is True
        assert result.task_type == "greenfield"
        assert result.prompt_section == ""
        assert result.impacts == []

    def test_modify_path_without_workspace_skips(self, tables):
        """Modify path detected but workspace_id missing -> skip cleanly."""
        from impact_analysis import run_impact_analysis  # type: ignore

        result = run_impact_analysis(
            issue_title="Rename log_audit_entry",
            issue_description=(
                "Rename python:lambda.shared.audit::"
                "log_audit_entry."
            ),
            plan_text="Step 1: rename it.",
            workspace_id="",
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert result.skipped is True
        assert result.task_type == "modify"
        assert "workspace_id" in result.reason

    def test_modify_path_no_symbols_skips(self, tables):
        """Modify-verb issue but no parseable symbols -> graceful skip."""
        from impact_analysis import run_impact_analysis  # type: ignore

        result = run_impact_analysis(
            issue_title="Fix a typo in the README",
            issue_description="Update the wording in step 3.",
            plan_text="Step 1: Update the README.",
            workspace_id=WORKSPACE_ID,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert result.skipped is True
        assert "no symbols resolved" in result.reason

    def test_log_audit_entry_signature_change_lists_all_9_callers(
        self, tables
    ):
        """**Chunk-07 Definition of Done.**

        A synthetic modify-task that changes the signature of
        ``log_audit_entry`` produces a plan that lists all 9
        caller updates.

        Mirrors the license-layer v2.0.0 migration the plan calls out as
        the proof case — see project_license_layer_v2.md in memory.
        """
        from impact_analysis import run_impact_analysis  # type: ignore

        _seed_log_audit_entry_callers(tables)

        result = run_impact_analysis(
            issue_title="Refactor log_audit_entry to accept repo_metadata",
            issue_description=(
                "Add a new repo_metadata parameter to "
                "python:lambda.shared.audit::log_audit_entry "
                "so each call can attach per-repo context. This is a "
                "signature change; callers need to be updated."
            ),
            plan_text=(
                "Step 1: Update python:lambda.shared.audit::"
                "log_audit_entry to accept the new parameter."
            ),
            workspace_id=WORKSPACE_ID,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

        assert result.skipped is False
        assert result.task_type == "modify"
        assert len(result.impacts) == 1
        impact = result.impacts[0]
        assert impact.target.symbol_id == REPORT_REPOS_SYMBOL
        assert impact.target.signature_change is True
        # The chunk-07 DoD: all 9 caller updates appear.
        assert len(impact.callers) == 9

        # And the prompt section is the artifact the model would receive.
        section = result.prompt_section
        assert "9 callers will need invocation updates" in section
        # Spot-check that each known caller is named in the section.
        for _, _, file_path, line in REPORT_REPOS_CALLERS:
            assert f"{file_path}:{line}" in section, (
                f"caller {file_path}:{line} missing from prompt section"
            )

    def test_graph_unavailable_does_not_raise(self, tables):
        """The reader raising SymbolGraphUnavailable must NOT propagate —
        impact analysis is best-effort per chunk-07's failure-modes table."""
        from impact_analysis import run_impact_analysis  # type: ignore
        import shared.symbol_graph_reader as reader  # type: ignore

        _seed_log_audit_entry_callers(tables)

        with patch.object(
            reader,
            "callers",
            side_effect=reader.SymbolGraphUnavailable("simulated"),
        ):
            result = run_impact_analysis(
                issue_title="Refactor log_audit_entry",
                issue_description=(
                    "Rename python:lambda.shared.audit::"
                    "log_audit_entry."
                ),
                plan_text="Step 1: rename the symbol.",
                workspace_id=WORKSPACE_ID,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                code_maps_table_name=CODE_MAPS_TABLE,
            )

        # Result returns; impact carries error metadata; no exception.
        assert result.skipped is False
        assert len(result.impacts) == 1
        assert result.impacts[0].error == "graph_unavailable"
        # Prompt section still renders, but tells the model the analysis
        # was skipped for this symbol.
        assert "symbol graph unavailable" in result.prompt_section
