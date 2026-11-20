# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for breaking_changes check handler.

Tests cover:
- Vector search querying via Lambda
- Handler name extraction from file paths
- Change type detection
- Handler summary filtering
- Confidence calculation
- Tailored query creation
- Main handler orchestration
"""

import json
import pytest
import sys
import os
import importlib.util
from unittest.mock import Mock, patch
from moto import mock_aws
import boto3
from botocore.exceptions import ClientError
from types import ModuleType, SimpleNamespace

# First, load bedrock_utils module (dependency for breaking_changes)
bedrock_utils_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/bedrock_utils.py'
)
bedrock_spec = importlib.util.spec_from_file_location("bedrock_utils", bedrock_utils_path)
bedrock_module = importlib.util.module_from_spec(bedrock_spec)
bedrock_spec.loader.exec_module(bedrock_module)

# Create a fake check_handlers package that contains bedrock_utils
check_handlers_package = ModuleType("check_handlers")
check_handlers_package.__path__ = [os.path.dirname(bedrock_utils_path)]
check_handlers_package.bedrock_utils = bedrock_module
sys.modules['check_handlers'] = check_handlers_package
sys.modules['check_handlers.bedrock_utils'] = bedrock_module

# Load the breaking_changes handler module as part of check_handlers package
handler_path = os.path.join(
    os.path.dirname(__file__),
    '../../process-pr-check/check_handlers/breaking_changes.py'
)
spec = importlib.util.spec_from_file_location(
    "check_handlers.breaking_changes",
    handler_path,
    submodule_search_locations=[os.path.dirname(handler_path)]
)
handler_module = importlib.util.module_from_spec(spec)
handler_module.__package__ = "check_handlers"
sys.modules['check_handlers.breaking_changes'] = handler_module
sys.modules['breaking_changes_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import classes and functions from loaded module
KbVectorResult = handler_module.KbVectorResult
Dependency = handler_module.Dependency
query_vector_search = handler_module.query_vector_search
extract_handler_name = handler_module.extract_handler_name
detect_change_type = handler_module.detect_change_type
filter_handler_summaries = handler_module.filter_handler_summaries
calculate_confidence = handler_module.calculate_confidence
create_tailored_query = handler_module.create_tailored_query
check_breaking_changes = handler_module.check_breaking_changes
symbol_graph_consumers = handler_module.symbol_graph_consumers
_plane1_file_scope = handler_module._plane1_file_scope


class TestKbVectorResult:
    """Test KbVectorResult data class"""

    def test_kb_vector_result_initialization(self):
        """Test: KbVectorResult initializes correctly"""
        # Act
        result = KbVectorResult(0.95, "handler text", "repo/handler.py")

        # Assert
        assert result.score == 0.95
        assert result.text == "handler text"
        assert result.source == "repo/handler.py"


class TestDependency:
    """Test Dependency data class"""

    def test_dependency_initialization(self):
        """Test: Dependency initializes correctly"""
        # Act
        dep = Dependency("hello", "Consumes hello events", "HIGH", "repo/consumer.py")

        # Assert
        assert dep.handler_name == "hello"
        assert dep.description == "Consumes hello events"
        assert dep.confidence == "HIGH"
        assert dep.source == "repo/consumer.py"


class TestQueryVectorSearch:
    """Test vector search querying via Lambda"""

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_success(self, mock_lambda):
        """Test: Successfully query vector search via query-kb raw mode"""
        # Arrange: query-kb returns an API-Gateway-style envelope whose body is
        # the raw ranked results (the standalone vector-query Lambda was folded
        # into query-kb, so there is no separate function to invoke).
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "statusCode": 200,
                "body": json.dumps({
                    "results": [
                        {"score": 0.95, "text": "handler text", "source": "repo/handler.py"}
                    ]
                }),
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        # Act
        results = query_vector_search("query-kb", "search query", 5, workspace_ids=["ws-1"])

        # Assert: parsed out of the envelope body
        assert len(results) == 1
        assert results[0].score == 0.95
        assert results[0].text == "handler text"
        assert results[0].source == "repo/handler.py"

        # Assert: invoked query-kb directly (not a removed vector-query) in raw mode,
        # scoped to the resolved workspace(s) and to handler-group-summaries only.
        _, kwargs = mock_lambda.invoke.call_args
        assert kwargs["FunctionName"] == "query-kb"
        sent = json.loads(kwargs["Payload"])
        assert sent["raw"] is True
        assert sent["topK"] == 5
        assert sent["workspaceIds"] == ["ws-1"]
        assert sent["docTypes"] == ["handler-group-summary"]

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_lambda_error(self, mock_lambda):
        """Test: Lambda invocation fails"""
        # Arrange
        mock_lambda.invoke.side_effect = ClientError(
            {"Error": {"Code": "ServiceException", "Message": "Service error"}},
            "invoke"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            query_vector_search("query-kb", "search query", 5, workspace_ids=["ws-1"])

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_non_200_raises(self, mock_lambda):
        """Test: a non-200 envelope from query-kb raw mode raises"""
        # Arrange
        mock_response = {
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "statusCode": 500,
                "body": json.dumps({"error": "boom"}),
            }).encode()))
        }
        mock_lambda.invoke.return_value = mock_response

        # Act & Assert
        with pytest.raises(RuntimeError):
            query_vector_search("query-kb", "search query", 5, workspace_ids=["ws-1"])

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_fails_closed_without_scope(self, mock_lambda):
        """Security: None/empty scope must NOT issue an unscoped cross-workspace query"""
        # Act: no scope (resolution failed) and empty scope (unmapped repo)
        none_results = query_vector_search("query-kb", "search query", 5, workspace_ids=None)
        empty_results = query_vector_search("query-kb", "search query", 5, workspace_ids=[])

        # Assert: zero results and query-kb was never invoked (fail closed)
        assert none_results == []
        assert empty_results == []
        mock_lambda.invoke.assert_not_called()

    @mock_aws()
    @patch('breaking_changes_handler.lambda_client')
    def test_query_vector_search_function_error_preserves_cause(self, mock_lambda):
        """Robustness: a Lambda FunctionError surfaces its payload, not 'Unknown error'"""
        # Arrange: unhandled exception in query-kb -> FunctionError + error payload
        mock_lambda.invoke.return_value = {
            "FunctionError": "Unhandled",
            "Payload": Mock(read=Mock(return_value=json.dumps({
                "errorType": "KeyError",
                "errorMessage": "'results'",
            }).encode())),
        }

        # Act & Assert: the cause ('results') is preserved in the exception
        with pytest.raises(RuntimeError, match="results"):
            query_vector_search("query-kb", "search query", 5, workspace_ids=["ws-1"])


class TestExtractHandlerName:
    """Test handler name extraction from file paths"""

    def test_extract_handler_name_lambda(self):
        """Test: Extract handler name from Lambda file path"""
        # Arrange
        file_path = "lambda/hello/handler.py"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "hello"

    def test_extract_handler_name_lambda_with_dash(self):
        """Test: Extract handler name with dashes"""
        # Arrange
        file_path = "lambda/analyze-pr/handler.py"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "analyze-pr"

    def test_extract_handler_name_terraform(self):
        """Test: Extract name from Terraform file"""
        # Arrange
        file_path = "terraform/lambda.tf"

        # Act
        result = extract_handler_name(file_path)

        # Assert
        assert result == "lambda"


class TestDetectChangeType:
    """Test change type detection"""

    def test_detect_change_type_infra(self):
        """Test: Detect infrastructure change"""
        # Arrange
        file_path = "terraform/lambda.tf"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "infra"

    def test_detect_change_type_logic(self):
        """Test: Detect logic change"""
        # Arrange
        file_path = "lambda/hello/handler.py"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "logic"

    def test_detect_change_type_schema(self):
        """Test: Detect schema change"""
        # Arrange
        file_path = "lambda/hello/schema.py"

        # Act
        result = detect_change_type(file_path)

        # Assert
        assert result == "schema"


class TestFilterHandlerSummaries:
    """Test filtering of handler summaries"""

    def test_filter_handler_summaries_keeps_handlers(self):
        """Test: Keep handler-group-summary results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "handler-group-summary: world")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 2

    def test_filter_handler_summaries_removes_code_maps(self):
        """Test: Remove code-map results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "code-map: architecture")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 1
        assert "hello" in filtered[0].source

    def test_filter_handler_summaries_removes_repo_overviews(self):
        """Test: Remove repo-overview results"""
        # Arrange
        results = [
            KbVectorResult(0.95, "text", "handler-group-summary: hello"),
            KbVectorResult(0.90, "text", "repo-overview: project")
        ]

        # Act
        filtered = filter_handler_summaries(results)

        # Assert
        assert len(filtered) == 1
        assert "hello" in filtered[0].source


class TestCalculateConfidence:
    """Test confidence calculation"""

    def test_calculate_confidence_high_with_queue(self):
        """Test: HIGH confidence with handler name + queue"""
        # Arrange
        handler_name = "hello"
        text = "This handler processes messages from the hello-queue using SQS"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "HIGH"

    def test_calculate_confidence_high_with_invoke(self):
        """Test: HIGH confidence with handler name + invoke"""
        # Arrange
        handler_name = "hello"
        text = "This service invokes the hello Lambda function"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "HIGH"

    def test_calculate_confidence_medium(self):
        """Test: MEDIUM confidence with generic dependency"""
        # Arrange
        handler_name = "hello"
        text = "This handler depends on hello for processing"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "MEDIUM"

    def test_calculate_confidence_low(self):
        """Test: LOW confidence with vague reference"""
        # Arrange
        handler_name = "hello"
        text = "Some unrelated text without specific references"

        # Act
        result = calculate_confidence(handler_name, text)

        # Assert
        assert result == "LOW"


class TestCreateTailoredQuery:
    """Test tailored query creation"""

    def test_create_tailored_query_schema(self):
        """Test: Create schema change query"""
        # Act
        result = create_tailored_query("hello", "schema")

        # Assert
        assert "consume" in result
        assert "hello" in result
        assert "schema" in result

    def test_create_tailored_query_logic(self):
        """Test: Create logic change query"""
        # Act
        result = create_tailored_query("hello", "logic")

        # Assert
        assert "invoke" in result or "depend" in result
        assert "hello" in result

    def test_create_tailored_query_infra(self):
        """Test: Create infrastructure change query"""
        # Act
        result = create_tailored_query("hello", "infra")

        # Assert
        assert "depend" in result
        assert "hello" in result
        assert "infrastructure" in result


class TestCheckBreakingChanges:
    """Test main check_breaking_changes handler"""

    def test_check_breaking_changes_no_relevant_files(self):
        """Test: No relevant files changed"""
        # Arrange
        changed_files = ["README.md", "docs/guide.md"]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            changed_files,
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        assert "No schema, logic, or infrastructure changes" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_high_confidence_found(self, mock_query):
        """Test: HIGH confidence dependencies found"""
        # Arrange
        mock_query.return_value = [
            KbVectorResult(
                0.95,
                "This handler processes messages from the hello queue using SQS",
                "handler-group-summary: consumer"
            )
        ]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "WARN"
        assert "consumer(s) detected" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_heuristic_findings_anchor_at_first_added_line(self, mock_query):
        """With a full added-line list, the heuristic anchors at the file's
        first added line and emits one finding per (file, handler)."""
        mock_query.return_value = [
            KbVectorResult(
                0.95,
                "This handler processes messages from the hello queue using SQS",
                "handler-group-summary: consumer"),
        ]
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "owner/repo",
            ["lambda/hello/handler.py"], "query-kb-lambda",
            anchors={"lambda/hello/handler.py": [12, 30, 45]})
        findings = result["findings"]
        assert len(findings) == 1  # one per (file, handler), not per consumer
        f = findings[0]
        assert f["path"] == "lambda/hello/handler.py"
        assert f["line"] == 12  # first added line, not 30/45
        assert "consumer(s)" in f["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_no_high_confidence(self, mock_query):
        """Test: No HIGH confidence dependencies found"""
        # Arrange
        mock_query.return_value = [
            KbVectorResult(
                0.80,
                "Some unrelated text",
                "handler-group-summary: other"
            )
        ]

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        assert "No dependencies found" in result["message"]

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_infra_only_skipped(self, mock_query):
        """Test: Infrastructure-only changes skip KB query"""
        # Arrange
        # Should not call query_vector_search for infra-only changes

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["terraform/lambda.tf"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        mock_query.assert_not_called()

    @patch('breaking_changes_handler.query_vector_search')
    def test_check_breaking_changes_query_error(self, mock_query):
        """Test: KB query error handled gracefully"""
        # Arrange
        mock_query.side_effect = Exception("KB error")

        # Act
        result = check_breaking_changes(
            "BREAKING_CHANGES",
            123,
            "owner/repo",
            ["lambda/hello/handler.py"],
            "query-kb-lambda"
        )

        # Assert
        assert result["checkType"] == "BREAKING_CHANGES"
        assert result["status"] == "PASS"
        # Should still return PASS since no high confidence dependencies were found


def _blk(removed=(), after=None, head=None):
    """Duck-typed shared.pr_diff.ChangeBlock test fixture."""
    return SimpleNamespace(
        removed_base_lines=tuple(removed),
        insert_after_base_line=after,
        head_anchor=head,
    )


class TestSymbolGraphConsumers:
    """Plane 1 (ADR-054): code-symbol consumer detection via the symbol graph."""

    class _FakeSGR:
        """Minimal stand-in for shared.symbol_graph_reader (injected via reader=).

        ``at_line`` is returned for every line; ``by_line`` (line -> sid or
        None) overrides it with per-line resolution. ``definitions_result``
        lists the sites that define a symbol; ``definitions_exc`` makes
        ``definitions`` raise.
        """

        class SymbolGraphUnavailable(Exception):
            pass

        def __init__(self, at_line=None, callers_result=None,
                     importers_result=None, raise_exc=None, by_line=None,
                     definitions_result=None, definitions_exc=None):
            self._at_line = at_line
            self._by_line = by_line
            self._callers = callers_result or SimpleNamespace(sites=[], complete=True)
            self._importers = importers_result or SimpleNamespace(sites=[], complete=True)
            self._definitions = definitions_result or SimpleNamespace(sites=[], complete=True)
            self._definitions_exc = definitions_exc
            self._raise = raise_exc
            self.lines_queried = []

        def symbol_at_line(self, ws, repo, file_path, line):
            if self._raise:
                raise self._raise
            self.lines_queried.append(line)
            if self._by_line is not None:
                sid = self._by_line.get(line)
                return SimpleNamespace(target_symbol_id=sid) if sid else None
            return self._at_line

        def callers(self, ws, sid):
            return self._callers

        def importers(self, ws, sid):
            return self._importers

        def definitions(self, ws, sid):
            if self._definitions_exc:
                raise self._definitions_exc
            return self._definitions

    def test_callers_and_importers_become_consumers(self):
        """A changed symbol's external callers + importers are reported."""
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg.mod::func"),
            callers_result=SimpleNamespace(
                sites=[SimpleNamespace(repo="other-repo",
                                       file_path="lambda/x/handler.py",
                                       line=42, source_symbol_id="x::f")],
                complete=True),
            importers_result=SimpleNamespace(
                sites=[SimpleNamespace(repo="other-repo",
                                       file_path="lambda/y/handler.py",
                                       line=7, source_symbol_id="y::g")],
                complete=True),
        )
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [_blk(removed=(10, 11), head=10)]}, reader=fake)

        assert undetermined == []
        assert sorted(c["relation"] for c in consumers) == ["caller", "importer"]
        assert {c["consumer_file"] for c in consumers} == {
            "lambda/x/handler.py", "lambda/y/handler.py"}
        assert all(c["changed_symbol_id"] == "pkg.mod::func" for c in consumers)

    def test_distinct_symbols_get_their_own_changed_line(self):
        """Two changed lines resolving different symbols -> each consumer finding
        carries the line that resolved its symbol (not the file's first line)."""
        outer = self

        class _MultiSymSGR(outer._FakeSGR):
            def symbol_at_line(self, ws, repo, file_path, line):
                return SimpleNamespace(target_symbol_id=f"pkg::sym{line}")

            def callers(self, ws, sid):
                return SimpleNamespace(
                    sites=[SimpleNamespace(repo="other", file_path=f"c/{sid}.py",
                                           line=1, source_symbol_id="x")],
                    complete=True)

            def importers(self, ws, sid):
                return SimpleNamespace(sites=[], complete=True)

        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"f.py": [_blk(removed=(10,), head=10),
                                     _blk(removed=(20,), head=20)]}, reader=_MultiSymSGR())
        assert undetermined == []
        by_line = {c["changed_line"]: c["changed_symbol_id"] for c in consumers}
        assert by_line == {10: "pkg::sym10", 20: "pkg::sym20"}

    def test_cross_workspace_consumer_found_via_scope_fanout(self):
        """ADR-056: a consumer whose caller edge lives in a DIFFERENT workspace
        than the changed symbol is found by querying every workspace in scope,
        not just the symbol's defining workspace."""
        outer = self

        class _XwsSGR(outer._FakeSGR):
            def symbol_at_line(self, ws, repo, file_path, line):
                # The changed symbol resolves only in the producer workspace.
                if ws == "ws-prod":
                    return SimpleNamespace(
                        target_symbol_id="shared_lib.math::add")
                return None

            def callers(self, ws, sid):
                # The cross-workspace consumer's edge lives in ws-consumer only.
                if ws == "ws-consumer":
                    return SimpleNamespace(
                        sites=[SimpleNamespace(
                            repo="eng-consumer", file_path="app/service.py",
                            line=17, source_symbol_id="app.service::process")],
                        complete=True)
                return SimpleNamespace(sites=[], complete=True)

            def importers(self, ws, sid):
                return SimpleNamespace(sites=[], complete=True)

        consumers, undetermined = symbol_graph_consumers(
            ["ws-prod", "ws-consumer"], "shared-lib",
            {"shared_lib/math.py": [_blk(removed=(4,), head=4)]}, reader=_XwsSGR())
        assert undetermined == []
        assert len(consumers) == 1
        assert consumers[0]["consumer_repo"] == "eng-consumer"
        assert consumers[0]["relation"] == "caller"
        assert consumers[0]["changed_symbol_id"] == "shared_lib.math::add"

    def test_self_references_excluded(self):
        """A caller inside the changed file itself is not a breaking consumer."""
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg.mod::func"),
            callers_result=SimpleNamespace(
                sites=[
                    SimpleNamespace(repo="myrepo",
                                    file_path="lambda/myrepo/handler.py",
                                    line=99, source_symbol_id="self::caller"),
                    SimpleNamespace(repo="other-repo",
                                    file_path="lambda/x/handler.py",
                                    line=42, source_symbol_id="x::f"),
                ], complete=True),
        )
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [_blk(removed=(10,), head=10)]}, reader=fake)
        assert undetermined == []
        assert len(consumers) == 1
        assert consumers[0]["consumer_repo"] == "other-repo"

    def test_test_file_consumers_excluded(self):
        """A consumer in a test file is expected-breakage, not a production
        signal -- filtered from the WARN. Only the production caller remains."""
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="go:binding::Default"),
            callers_result=SimpleNamespace(
                sites=[
                    SimpleNamespace(repo="gin",
                                    file_path="binding/binding_test.go",
                                    line=158, source_symbol_id="t::TestBindingDefault"),
                    SimpleNamespace(repo="gin",
                                    file_path="context.go",
                                    line=780, source_symbol_id="go:context.go::Context::Bind"),
                ], complete=True),
        )
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "gin", {"binding/binding.go": [_blk(removed=(95,), head=95)]}, reader=fake)
        assert undetermined == []
        assert len(consumers) == 1
        assert consumers[0]["consumer_file"] == "context.go"

    def test_no_symbol_resolved_is_not_undetermined(self):
        """No enclosing symbol (comment/whitespace) -> no consumers, NOT fail-closed."""
        fake = self._FakeSGR(at_line=None)
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [_blk(removed=(3,), head=3)]}, reader=fake)
        assert consumers == []
        assert undetermined == []

    def test_graph_unavailable_fails_closed(self):
        """Graph unavailable -> file undetermined (manual review), never silent safe."""
        fake = self._FakeSGR(raise_exc=self._FakeSGR.SymbolGraphUnavailable("down"))
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [_blk(removed=(10,), head=10)]}, reader=fake)
        assert consumers == []
        assert undetermined == ["lambda/myrepo/handler.py"]

    def test_reader_error_fails_closed(self):
        """Any reader error -> fail closed to undetermined."""
        fake = self._FakeSGR(raise_exc=RuntimeError("boom"))
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [_blk(removed=(10,), head=10)]}, reader=fake)
        assert consumers == []
        assert undetermined == ["lambda/myrepo/handler.py"]


    @staticmethod
    def _caller_site(repo, file_path, line=1):
        return SimpleNamespace(repo=repo, file_path=file_path, line=line,
                               source_symbol_id="x::caller")

    def _one_caller_reader(self, by_line, caller_repo="other-repo"):
        return self._FakeSGR(
            by_line=by_line,
            callers_result=SimpleNamespace(
                sites=[self._caller_site(caller_repo, "app/use.py", 9)],
                complete=True),
        )

    def test_replacement_block_resolves_base_line_not_head_line(self):
        """The graph reflects the base branch: a replacement block resolves its
        REMOVED BASE line, and the HEAD line number is only the anchor."""
        fake = self._one_caller_reader({40: "pkg::changed", 55: "pkg::unrelated"})
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(removed=(40,), head=55)]}, reader=fake)
        assert undetermined == []
        assert fake.lines_queried == [40]
        assert {c["changed_symbol_id"] for c in consumers} == {"pkg::changed"}
        assert consumers[0]["changed_line"] == 55

    def test_pure_insertion_inside_function_flags_it(self):
        """Insertion after BASE line L where L and L + 1 resolve to the SAME
        symbol is inside that symbol's body -> the symbol is changed."""
        fake = self._one_caller_reader({12: "pkg::f", 13: "pkg::f"})
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(after=12, head=14)]}, reader=fake)
        assert undetermined == []
        assert [c["changed_symbol_id"] for c in consumers] == ["pkg::f"]
        assert consumers[0]["changed_line"] == 14

    def test_pure_insertion_between_functions_flags_nothing(self):
        """L and L + 1 resolve to different symbols (insertion between two
        functions) -> no changed symbol, not undetermined."""
        fake = self._one_caller_reader({12: "pkg::f", 13: "pkg::g"})
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(after=12, head=14)]}, reader=fake)
        assert consumers == []
        assert undetermined == []

    def test_pure_insertion_missing_side_flags_nothing(self):
        """One side unresolved (e.g. end of file) -> skip the block."""
        fake = self._one_caller_reader({12: "pkg::f"})
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(after=12, head=14)]}, reader=fake)
        assert consumers == []
        assert undetermined == []

    def test_insertion_at_top_of_file_flags_nothing(self):
        """insert_after_base_line=0 (top of file) never resolves a symbol."""
        fake = self._one_caller_reader({0: "pkg::f", 1: "pkg::f"})
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(after=0, head=1)]}, reader=fake)
        assert consumers == []
        assert undetermined == []
        assert fake.lines_queried == []

    def test_other_repo_defining_own_copy_is_dropped(self):
        """A caller in another repo that also DEFINES the sid calls its own
        copy (ids are not repo-qualified) -> dropped. A caller in a repo that
        does not define it, and a same-repo caller in another file, are kept."""
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg.mod::func"),
            callers_result=SimpleNamespace(sites=[
                self._caller_site("fork-repo", "pkg/user.py", 3),
                self._caller_site("consumer-repo", "app/use.py", 4),
                self._caller_site("myrepo", "pkg/other.py", 5),
            ], complete=True),
            definitions_result=SimpleNamespace(sites=[
                self._caller_site("myrepo", "pkg/mod.py", 1),
                self._caller_site("fork-repo", "pkg/mod.py", 1),
            ], complete=True),
        )
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"pkg/mod.py": [_blk(removed=(1,), head=1)]},
            reader=fake)
        assert undetermined == []
        assert {c["consumer_repo"] for c in consumers} == {"consumer-repo", "myrepo"}

    def test_defining_repos_cached_per_symbol(self):
        """definitions() is queried once per (sid, ws) for the whole run."""
        calls = []
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::f"),
            callers_result=SimpleNamespace(sites=[
                self._caller_site("a", "x.py"), self._caller_site("b", "y.py"),
            ], complete=True),
        )
        orig = fake.definitions

        def _counting(ws, sid):
            calls.append((ws, sid))
            return orig(ws, sid)

        fake.definitions = _counting
        consumers, _ = symbol_graph_consumers(
            ["ws1", "ws2"], "myrepo",
            {"m1.py": [_blk(removed=(1,), head=1)],
             "m2.py": [_blk(removed=(1,), head=1)]}, reader=fake)
        assert sorted(calls) == [("ws1", "pkg::f"), ("ws2", "pkg::f")]
        assert {c["consumer_repo"] for c in consumers} == {"a", "b"}

    def test_definitions_error_fails_closed(self):
        """definitions() raising -> the file is undetermined (manual review)."""
        fake = self._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::f"),
            callers_result=SimpleNamespace(
                sites=[self._caller_site("other-repo", "app/use.py")],
                complete=True),
            definitions_exc=self._FakeSGR.SymbolGraphUnavailable("down"),
        )
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"mod.py": [_blk(removed=(1,), head=1)]},
            reader=fake)
        assert consumers == []
        assert undetermined == ["mod.py"]

    def test_deletion_only_block_falls_back_to_first_added_line(self):
        """A block with no HEAD anchor uses the file's first added line."""
        fake = self._one_caller_reader({7: "pkg::f"})
        consumers, _ = symbol_graph_consumers(
            ["ws1"], "myrepo", {"mod.py": [_blk(removed=(7,))]},
            first_added_by_file={"mod.py": 30}, reader=fake)
        assert consumers[0]["changed_line"] == 30

    def test_deletion_only_file_has_no_anchor(self):
        """No HEAD anchor and no added line in the file -> changed_line None."""
        fake = self._one_caller_reader({7: "pkg::f"})
        consumers, _ = symbol_graph_consumers(
            ["ws1"], "myrepo", {"mod.py": [_blk(removed=(7,))]}, reader=fake)
        assert len(consumers) == 1
        assert consumers[0]["changed_line"] is None

    def test_first_non_none_anchor_kept_per_symbol(self):
        """Two blocks in the same symbol: the first anchor seen is kept."""
        fake = self._one_caller_reader({7: "pkg::f", 9: "pkg::f"})
        consumers, _ = symbol_graph_consumers(
            ["ws1"], "myrepo",
            {"mod.py": [_blk(removed=(7,)), _blk(removed=(9,), head=11),
                        _blk(removed=(9,), head=20)]}, reader=fake)
        assert consumers[0]["changed_line"] == 11


class TestPlane1FileScope:
    """Plane-1 file scope is language- and repo-agnostic: keyed on source-code
    extension (any symbol-graph language), not a lambda/*.py path."""

    def test_includes_shared_and_non_handler_modules(self):
        """shared/*.py and non-handler lambda modules are in plane-1 scope --
        exactly the high-fan-out code the handler-only heuristic missed."""
        changed = [
            "lambda/shared/pr_metadata.py",
            "lambda/shared/model_client.py",
            "lambda/process-pr-check/check_handlers/breaking_changes.py",
            "lambda/analyze-pr/diff_symbol_resolver.py",
        ]
        assert _plane1_file_scope(changed) == changed

    def test_includes_handler_py(self):
        """handler.py is still Python -> stays in plane-1 scope (handlers can be
        imported too; widening is additive, not a swap)."""
        assert _plane1_file_scope(["lambda/hello/handler.py"]) == [
            "lambda/hello/handler.py"
        ]

    def test_includes_all_symbol_graph_languages_any_path(self):
        """A changed source file of ANY symbol-graph language is in scope,
        regardless of repo layout (Go/C#/TS/TSX/Java/ABAP, not under lambda/)."""
        changed = [
            "binding/binding.go",                         # Go, repo root pkg
            "src/ClientApp/ViewModels/CatalogItemViewModel.cs",  # C#
            "src/utils/accept.ts",                        # TS
            "ui/src/App.tsx",                             # TSX
            "src/main/java/com/acme/Widget.java",         # Java
            "src/zcl_thing.abap",                         # ABAP
        ]
        assert _plane1_file_scope(changed) == changed

    def test_excludes_tests_all_conventions(self):
        """Test modules carry no production consumers and are excluded across
        per-language conventions (dir-based and co-located)."""
        assert _plane1_file_scope([
            "lambda/tests/unit/test_check_breaking_changes.py",
            "lambda/shared/tests/test_pr_metadata.py",
            "binding/form_mapping_test.go",               # Go co-located
            "src/utils/accept.test.ts",                   # TS co-located
            "src/router/trie.spec.ts",
            "tests/Basket.UnitTests/BasketServiceTests.cs",  # C#
            "src/test/java/com/acme/WidgetTest.java",
        ]) == []

    def test_excludes_non_code(self):
        """Terraform, config, docs and other non-code files carry no resolvable
        symbol and are not plane-1 targets."""
        assert _plane1_file_scope([
            "terraform/lambda.tf",
            "lambda/hello/config.yaml",
            "go.mod",
            "package.json",
            "README.md",
        ]) == []


class TestPlane1ScopeWidening:
    """a shared/*.py-only PR now reaches plane 1 instead of an early
    PASS, and its real cross-file consumers surface as line-anchored WARN
    findings. Scope resolution is patched and the symbol-graph reader is
    injected, so no live AWS is touched."""

    def _reader_with_caller(self, consumer_file):
        return TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="python:lambda.shared.pr_metadata::fetch_pr_metadata"),
            callers_result=SimpleNamespace(
                sites=[SimpleNamespace(
                    repo="outcome-ops-ai-assist",
                    file_path=consumer_file, line=88,
                    source_symbol_id="python:lambda.process-pr-check.handler::run")],
                complete=True),
        )

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_shared_only_pr_runs_plane1_and_flags_consumer(self, _mock_scope):
        """shared/*.py change with a graph consumer -> WARN, finding anchored on
        the changed def line, naming the consuming file. Baseline behavior would
        have early-returned PASS (no handler.py / .tf in the diff)."""
        reader = self._reader_with_caller("lambda/process-pr-check/handler.py")
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/pr_metadata.py"], "query-kb-lambda",
            anchors={"lambda/shared/pr_metadata.py": [10, 25]},
            change_blocks={"lambda/shared/pr_metadata.py": [
                _blk(removed=(10,), head=10), _blk(removed=(25,), head=25)]},
            reader=reader,
        )
        assert result["status"] == "WARN"
        findings = result["findings"]
        assert len(findings) == 1
        f = findings[0]
        assert f["path"] == "lambda/shared/pr_metadata.py"
        assert f["line"] == 10          # the changed line that resolved the symbol
        assert "process-pr-check/handler.py" in f["message"]
        assert "fetch_pr_metadata" in f["message"]

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_shared_only_pr_no_consumers_is_pass(self, _mock_scope):
        """shared/*.py change with NO graph consumers -> genuine PASS (true safe),
        not an early-exit PASS: plane 1 actually ran."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="python:lambda.shared.x::y"),
        )  # empty callers + importers
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/x.py"], "query-kb-lambda",
            anchors={"lambda/shared/x.py": [5]},
            change_blocks={"lambda/shared/x.py": [_blk(removed=(5,), head=5)]},
            reader=reader,
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []
        assert any("Scanned 1 code module" in d for d in result["details"])

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_shared_only_graph_unavailable_fails_closed(self, _mock_scope):
        """Graph unavailable on a shared-only PR -> WARN (manual review), never a
        silent safe PASS (ADR-054 fail-closed)."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            raise_exc=TestSymbolGraphConsumers._FakeSGR.SymbolGraphUnavailable("down"))
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/x.py"], "query-kb-lambda",
            anchors={"lambda/shared/x.py": [5]},
            change_blocks={"lambda/shared/x.py": [_blk(removed=(5,), head=5)]},
            reader=reader,
        )
        assert result["status"] == "WARN"
        assert any("manual review" in d.lower() for d in result["details"])

    def test_handler_only_pr_without_anchors_unchanged(self):
        """Regression: a handler.py-only PR with no anchors and no resolved
        workspace scope keeps the heuristic path and does not invoke plane 1."""
        with patch('breaking_changes_handler.query_vector_search', return_value=[]):
            result = check_breaking_changes(
                "BREAKING_CHANGES", 123, "owner/repo",
                ["lambda/hello/handler.py"], "query-kb-lambda",
            )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_no_change_blocks_fails_closed(self, _mock_scope):
        """change_blocks=None with plane-1 files -> every plane-1 file is a
        manual-review WARN, never silently skipped."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::f"))
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/x.py", "lambda/shared/y.py"], "query-kb-lambda",
            anchors={"lambda/shared/x.py": [5]},
            change_blocks=None, reader=reader,
        )
        assert result["status"] == "WARN"
        assert result["findings"] == []
        assert reader.lines_queried == []
        text = "\n".join(result["details"])
        assert "lambda/shared/x.py" in text and "lambda/shared/y.py" in text
        assert "manual review" in text.lower()

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_file_missing_from_change_blocks_fails_closed(self, _mock_scope):
        """A plane-1 file the worker could not diff (absent from
        change_blocks) is a manual-review WARN, never treated as unchanged."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::f"))
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/x.py", "lambda/shared/y.py"], "query-kb-lambda",
            anchors={},
            change_blocks={"lambda/shared/x.py": []},
            reader=reader,
        )
        assert result["status"] == "WARN"
        text = "\n".join(result["details"])
        assert "lambda/shared/y.py" in text
        assert "lambda/shared/x.py" not in text.split("Could not determine")[-1]
        assert "manual review" in text.lower()

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_deletion_only_consumer_in_details_without_finding(self, _mock_scope):
        """Deletion-only block in a file with no added lines: the consumer is
        listed in details, but no Finding is emitted (ADR-031)."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::gone"),
            callers_result=SimpleNamespace(sites=[SimpleNamespace(
                repo="consumer", file_path="app/use.py", line=3,
                source_symbol_id="x")], complete=True),
        )
        result = check_breaking_changes(
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
            ["lambda/shared/x.py"], "query-kb-lambda",
            anchors={},
            change_blocks={"lambda/shared/x.py": [_blk(removed=(8, 9))]},
            reader=reader,
        )
        assert result["status"] == "WARN"
        assert result["findings"] == []
        assert any("consumer/app/use.py:3" in d for d in result["details"])

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_plane1_warn_drops_plane2_pass_text(self, _mock_scope):
        """With plane-1 consumers on a handler PR whose plane 2 found nothing,
        the plane-2 'safe' lines are removed; the Modified line stays."""
        reader = TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="pkg::handler"),
            callers_result=SimpleNamespace(sites=[SimpleNamespace(
                repo="consumer", file_path="app/use.py", line=3,
                source_symbol_id="x")], complete=True),
        )
        with patch('breaking_changes_handler.query_vector_search', return_value=[]):
            result = check_breaking_changes(
                "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
                ["lambda/hello/handler.py"], "query-kb-lambda",
                anchors={"lambda/hello/handler.py": [4]},
                change_blocks={"lambda/hello/handler.py": [_blk(removed=(4,), head=4)]},
                reader=reader,
            )
        assert result["status"] == "WARN"
        details = result["details"]
        assert "Changes appear safe to merge." not in details
        assert "No direct consumers detected in knowledge base." not in details
        assert "These may be new handlers, internal-only, or not yet documented." not in details
        assert any(d.startswith("Modified 1 file(s)") for d in details)
        assert len(result["findings"]) == 1

    @patch('shared.pr_check_scope.resolve_pr_check_workspace_scope',
           return_value=["ws-eng"])
    def test_plane1_no_consumers_keeps_plane2_pass_text(self, _mock_scope):
        """No plane-1 signal -> the plane-2 PASS wording is untouched."""
        reader = TestSymbolGraphConsumers._FakeSGR(at_line=None)
        with patch('breaking_changes_handler.query_vector_search', return_value=[]):
            result = check_breaking_changes(
                "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist",
                ["lambda/hello/handler.py"], "query-kb-lambda",
                anchors={"lambda/hello/handler.py": [4]},
                change_blocks={"lambda/hello/handler.py": [_blk(removed=(4,), head=4)]},
                reader=reader,
            )
        assert result["status"] == "PASS"
        assert "Changes appear safe to merge." in result["details"]
