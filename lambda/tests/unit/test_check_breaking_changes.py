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


class TestSymbolGraphConsumers:
    """Plane 1 (ADR-054): code-symbol consumer detection via the symbol graph."""

    class _FakeSGR:
        """Minimal stand-in for shared.symbol_graph_reader (injected via reader=)."""

        class SymbolGraphUnavailable(Exception):
            pass

        def __init__(self, at_line=None, callers_result=None,
                     importers_result=None, raise_exc=None):
            self._at_line = at_line
            self._callers = callers_result or SimpleNamespace(sites=[], complete=True)
            self._importers = importers_result or SimpleNamespace(sites=[], complete=True)
            self._raise = raise_exc

        def symbol_at_line(self, ws, repo, file_path, line):
            if self._raise:
                raise self._raise
            return self._at_line

        def callers(self, ws, sid):
            return self._callers

        def importers(self, ws, sid):
            return self._importers

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
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [10, 11]}, reader=fake)

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
            ["ws1"], "myrepo", {"f.py": [10, 20]}, reader=_MultiSymSGR())
        assert undetermined == []
        by_line = {c["changed_line"]: c["changed_symbol_id"] for c in consumers}
        assert by_line == {10: "pkg::sym10", 20: "pkg::sym20"}

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
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [10]}, reader=fake)
        assert undetermined == []
        assert len(consumers) == 1
        assert consumers[0]["consumer_repo"] == "other-repo"

    def test_no_symbol_resolved_is_not_undetermined(self):
        """No enclosing symbol (comment/whitespace) -> no consumers, NOT fail-closed."""
        fake = self._FakeSGR(at_line=None)
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [3]}, reader=fake)
        assert consumers == []
        assert undetermined == []

    def test_graph_unavailable_fails_closed(self):
        """Graph unavailable -> file undetermined (manual review), never silent safe."""
        fake = self._FakeSGR(raise_exc=self._FakeSGR.SymbolGraphUnavailable("down"))
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [10]}, reader=fake)
        assert consumers == []
        assert undetermined == ["lambda/myrepo/handler.py"]

    def test_reader_error_fails_closed(self):
        """Any reader error -> fail closed to undetermined."""
        fake = self._FakeSGR(raise_exc=RuntimeError("boom"))
        consumers, undetermined = symbol_graph_consumers(
            ["ws1"], "myrepo", {"lambda/myrepo/handler.py": [10]}, reader=fake)
        assert consumers == []
        assert undetermined == ["lambda/myrepo/handler.py"]


class TestPlane1FileScope:
    """Chunk 11: plane-1 file scope is widened beyond handler.py."""

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
        """handler.py is still Python under lambda/ -> stays in plane-1 scope
        (handlers can be imported too; widening is additive, not a swap)."""
        assert _plane1_file_scope(["lambda/hello/handler.py"]) == [
            "lambda/hello/handler.py"
        ]

    def test_excludes_tests(self):
        """Test modules carry no production consumers and are excluded."""
        assert _plane1_file_scope([
            "lambda/tests/unit/test_check_breaking_changes.py",
            "lambda/shared/tests/test_pr_metadata.py",
        ]) == []

    def test_excludes_terraform_non_lambda_and_non_python(self):
        """Terraform (no Python symbol), non-lambda paths, and non-.py files
        are not plane-1 targets."""
        assert _plane1_file_scope([
            "terraform/lambda.tf",
            "ui/src/app.ts",
            "lambda/hello/config.yaml",
            "README.md",
        ]) == []


class TestPlane1ScopeWidening:
    """Chunk 11: a shared/*.py-only PR now reaches plane 1 instead of an early
    PASS, and its real cross-file consumers surface as line-anchored WARN
    findings. Scope resolution is patched and the symbol-graph reader is
    injected, so no live AWS is touched."""

    def _reader_with_caller(self, consumer_file):
        return TestSymbolGraphConsumers._FakeSGR(
            at_line=SimpleNamespace(target_symbol_id="python:lambda.shared.pr_metadata::fetch_pr_metadata"),
            callers_result=SimpleNamespace(
                sites=[SimpleNamespace(
                    repo="outcome-ops-ai-assist-private",
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
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist-private",
            ["lambda/shared/pr_metadata.py"], "query-kb-lambda",
            anchors={"lambda/shared/pr_metadata.py": [10, 25]},
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
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist-private",
            ["lambda/shared/x.py"], "query-kb-lambda",
            anchors={"lambda/shared/x.py": [5]},
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
            "BREAKING_CHANGES", 123, "bcarpio/outcome-ops-ai-assist-private",
            ["lambda/shared/x.py"], "query-kb-lambda",
            anchors={"lambda/shared/x.py": [5]},
            reader=reader,
        )
        assert result["status"] == "WARN"
        assert any("manual review" in d.lower() for d in result["details"])

    def test_handler_only_pr_without_anchors_unchanged(self):
        """Regression: a handler.py-only PR with no anchors keeps the heuristic
        path and does not invoke plane 1 (no anchors -> no symbol resolution)."""
        with patch('breaking_changes_handler.query_vector_search', return_value=[]):
            result = check_breaking_changes(
                "BREAKING_CHANGES", 123, "owner/repo",
                ["lambda/hello/handler.py"], "query-kb-lambda",
            )
        assert result["status"] == "PASS"
        assert result["findings"] == []
