"""Unit tests for OutcomeOps MCP server."""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# Set required environment variables before importing
os.environ["DYNAMODB_TABLE"] = "test-table"
os.environ["S3_VECTORS_BUCKET"] = "test-vectors-bucket"
os.environ["S3_VECTORS_INDEX"] = "test-vectors-index"
os.environ["KB_BUCKET"] = "test-kb-bucket"
os.environ["ENVIRONMENT"] = "test"


def _load_mcp_module():
    """Load the MCP server handler module."""
    if 'mcp_server_handler' in sys.modules:
        del sys.modules['mcp_server_handler']
    handler_path = os.path.join(os.path.dirname(__file__), '../../mcp-server/handler.py')
    spec = importlib.util.spec_from_file_location("mcp_server_handler", handler_path)
    mcp_module = importlib.util.module_from_spec(spec)
    sys.modules['mcp_server_handler'] = mcp_module
    spec.loader.exec_module(mcp_module)
    return mcp_module


mcp_module = _load_mcp_module()


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource."""
    table = MagicMock()
    with patch.object(mcp_module, '_get_dynamodb') as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        yield table


@pytest.fixture
def mock_s3vectors():
    """Mock S3 Vectors client."""
    client = MagicMock()
    with patch.object(mcp_module, '_get_s3vectors_client', return_value=client):
        yield client


@pytest.fixture
def mock_bedrock():
    """Mock Bedrock client."""
    client = MagicMock()
    # Return a mock embedding
    embedding_response = MagicMock()
    embedding_response.read.return_value = json.dumps({
        "embedding": [0.1] * 1024
    }).encode()
    client.invoke_model.return_value = {"body": embedding_response}
    with patch.object(mcp_module, '_get_bedrock_client', return_value=client):
        yield client


class TestValidateApiKey:
    """Test API key validation in the MCP server."""

    def test_valid_key(self, mock_dynamodb):
        """Test validation of an active, non-expired key."""
        test_key = "ooa_abc123def456abc123def456abc123def456abcd"
        key_hash = hashlib.sha256(test_key.encode()).hexdigest()

        mock_dynamodb.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#key-1",
                "key_id": "key-1",
                "status": "active",
            }]
        }
        mock_dynamodb.update_item.return_value = {}

        result = mcp_module.validate_api_key(test_key)
        assert result == "user@example.com"

    def test_missing_key(self, mock_dynamodb):
        """Test validation of a non-existent key."""
        mock_dynamodb.query.return_value = {"Items": []}

        result = mcp_module.validate_api_key("ooa_nonexistent1234567890abcdef123456ab")
        assert result is None

    def test_expired_key(self, mock_dynamodb):
        """Test validation of an expired key."""
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        mock_dynamodb.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#key-1",
                "status": "active",
                "expires_at": past,
            }]
        }

        result = mcp_module.validate_api_key("ooa_expired1234567890abcdef1234567890ab")
        assert result is None

    def test_revoked_key(self, mock_dynamodb):
        """Test validation of a revoked key."""
        mock_dynamodb.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#key-1",
                "status": "revoked",
            }]
        }

        result = mcp_module.validate_api_key("ooa_revoked1234567890abcdef1234567890ab")
        assert result is None

    def test_empty_key(self, mock_dynamodb):
        """Test validation with empty key string."""
        result = mcp_module.validate_api_key("")
        assert result is None


class TestMcpToolHandlers:
    """Test MCP tool handler functions."""

    def test_query_knowledge_base(self, mock_dynamodb, mock_s3vectors, mock_bedrock):
        """Test query_knowledge_base tool."""
        # Mock workspace lookup
        mock_dynamodb.get_item.return_value = {
            "Item": {"org_role": "org_admin"}
        }
        mock_dynamodb.scan.return_value = {
            "Items": [{"PK": "WS#ws-1"}]
        }

        # Mock vector search results
        mock_s3vectors.query_vectors.return_value = {
            "vectors": [{
                "key": "doc-1",
                "distance": 0.85,
                "metadata": {
                    "content": "Lambda handlers should use structured logging.",
                    "repo": "my-repo",
                    "type": "adr",
                    "file_path": "docs/adr/ADR-001.md",
                    "workspace_id": "ws-1",
                },
            }]
        }

        result = mcp_module.handle_query_knowledge_base(
            {"query": "Lambda handler patterns"}, "admin@example.com"
        )

        assert "text" in result
        assert "Lambda handlers should use structured logging" in result["text"]
        assert "my-repo" in result["text"]

    def test_query_knowledge_base_missing_query(self, mock_dynamodb):
        """Test query_knowledge_base with missing query."""
        result = mcp_module.handle_query_knowledge_base({}, "user@example.com")
        assert "error" in result

    def test_query_knowledge_base_with_workspace(self, mock_dynamodb, mock_s3vectors, mock_bedrock):
        """Test query_knowledge_base with explicit workspace_id."""
        mock_s3vectors.query_vectors.return_value = {"vectors": []}

        result = mcp_module.handle_query_knowledge_base(
            {"query": "test", "workspace_id": "ws-123"}, "user@example.com"
        )

        assert "text" in result
        # Verify workspace filter was applied
        call_kwargs = mock_s3vectors.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"workspace_id": {"$eq": "ws-123"}}

    def test_list_workspaces(self, mock_dynamodb):
        """Test list_workspaces tool."""
        # User is not org admin
        mock_dynamodb.get_item.side_effect = lambda **kwargs: (
            {"Item": {"org_role": "member"}} if kwargs["Key"]["SK"] == "ORG_MEMBER"
            else {"Item": {"name": "My Workspace", "description": "Test workspace"}}
        )
        mock_dynamodb.query.return_value = {
            "Items": [{"workspace_id": "ws-1"}]
        }
        # Share-resolution path in get_user_workspace_ids paginates table.scan
        # via LastEvaluatedKey; the test must return a real dict (no key) so
        # the loop exits. A bare MagicMock would return a truthy MagicMock and
        # spin forever.
        mock_dynamodb.scan.return_value = {"Items": []}

        result = mcp_module.handle_list_workspaces({}, "user@example.com")

        assert "text" in result
        assert "My Workspace" in result["text"]

    def test_list_workspaces_empty(self, mock_dynamodb):
        """Test list_workspaces when user has no workspaces."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": []}
        # Chunk 01 of workspace-scope plan: handler now scans for
        # is_global workspaces to append. Return an empty page so the
        # scan loop exits cleanly instead of hitting a MagicMock's
        # truthy LastEvaluatedKey and spinning forever.
        mock_dynamodb.scan.return_value = {"Items": []}

        result = mcp_module.handle_list_workspaces({}, "user@example.com")

        assert "text" in result
        assert "no workspaces" in result["text"].lower()

    def test_list_workspace_documents(self, mock_dynamodb, mock_s3vectors, mock_bedrock):
        """list_workspace_documents queries the vector store with a
        workspace filter, dedupes chunks by (repo, file_path), and
        returns one entry per distinct document.
        """
        # Mock workspace access check
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": [{"workspace_id": "ws-1"}]}
        mock_dynamodb.scan.return_value = {"Items": []}

        # Two chunks of the same doc + one separate doc + one code-map.
        # Listing should dedupe ADR-001 down to a single row.
        mock_s3vectors.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-1-chunk-0",
                    "metadata": {
                        "workspace_id": "ws-1",
                        "repo": "my-repo",
                        "type": "adr",
                        "file_path": "docs/adr/ADR-001.md",
                    },
                },
                {
                    "key": "doc-1-chunk-1",
                    "metadata": {
                        "workspace_id": "ws-1",
                        "repo": "my-repo",
                        "type": "adr",
                        "file_path": "docs/adr/ADR-001.md",
                    },
                },
                {
                    "key": "doc-2",
                    "metadata": {
                        "workspace_id": "ws-1",
                        "repo": "my-repo",
                        "type": "markdown",
                        "file_path": "README.md",
                    },
                },
                {
                    "key": "code-map-1",
                    "metadata": {
                        "workspace_id": "ws-1",
                        "repo": "my-repo",
                        "type": "code-map",
                        "file_path": "architectural-summary.txt",
                    },
                },
            ]
        }

        result = mcp_module.handle_list_workspace_documents(
            {"workspace_id": "ws-1"}, "user@example.com"
        )

        assert "text" in result
        # Three distinct documents (ADR-001 deduped from two chunks).
        assert "Documents in workspace (3)" in result["text"]
        assert "docs/adr/ADR-001.md" in result["text"]
        assert "README.md" in result["text"]
        assert "architectural-summary.txt" in result["text"]
        assert "type: code-map" in result["text"]

        # Verify the vector store call used the workspace_id filter.
        call_kwargs = mock_s3vectors.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"workspace_id": {"$eq": "ws-1"}}
        assert call_kwargs["returnMetadata"] is True

    def test_list_workspace_documents_empty(self, mock_dynamodb, mock_s3vectors, mock_bedrock):
        """Empty vector result surfaces 'No documents found'."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": [{"workspace_id": "ws-1"}]}
        mock_dynamodb.scan.return_value = {"Items": []}
        mock_s3vectors.query_vectors.return_value = {"vectors": []}

        result = mcp_module.handle_list_workspace_documents(
            {"workspace_id": "ws-1"}, "user@example.com"
        )

        assert "text" in result
        assert "No documents found" in result["text"]

    def test_list_workspace_documents_no_access(self, mock_dynamodb):
        """Test list_workspace_documents when user lacks access."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": []}

        result = mcp_module.handle_list_workspace_documents(
            {"workspace_id": "ws-999"}, "user@example.com"
        )

        assert "error" in result

    def test_list_workspace_documents_missing_id(self, mock_dynamodb):
        """Test list_workspace_documents without workspace_id."""
        result = mcp_module.handle_list_workspace_documents({}, "user@example.com")
        assert "error" in result

    def test_get_coding_standards(self, mock_dynamodb, mock_s3vectors, mock_bedrock):
        """Test get_coding_standards tool."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "org_admin"}}
        mock_dynamodb.scan.return_value = {"Items": [{"PK": "WS#ws-1"}]}

        mock_s3vectors.query_vectors.return_value = {
            "vectors": [{
                "key": "doc-1",
                "distance": 0.9,
                "metadata": {
                    "content": "Always use Terraform fmt before committing.",
                    "repo": "infra-repo",
                    "type": "standard",
                },
            }]
        }

        result = mcp_module.handle_get_coding_standards(
            {"topic": "Terraform"}, "admin@example.com"
        )

        assert "text" in result
        assert "Terraform fmt" in result["text"]

    def test_get_coding_standards_missing_topic(self, mock_dynamodb):
        """Test get_coding_standards without topic."""
        result = mcp_module.handle_get_coding_standards({}, "user@example.com")
        assert "error" in result


class TestMcpProtocol:
    """Test MCP JSON-RPC protocol helpers."""

    def test_jsonrpc_response(self):
        """Test JSON-RPC success response format."""
        result = mcp_module.jsonrpc_response(1, {"tools": []})
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    def test_jsonrpc_error(self):
        """Test JSON-RPC error response format."""
        result = mcp_module.jsonrpc_error(1, -32601, "Method not found")
        assert result == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }

    def test_jsonrpc_error_with_data(self):
        """Test JSON-RPC error response with data."""
        result = mcp_module.jsonrpc_error(1, -32603, "Internal error", {"detail": "something"})
        assert result["error"]["data"] == {"detail": "something"}


class TestMcpTools:
    """Test MCP tool definitions."""

    def test_all_tools_defined(self):
        """Test that the expected native tools are all defined.

        Six symbol-graph query tools (the five directional/resolution
        tools plus ``find_symbol_at_line`` for line-citation grounding
        per ADR-031) plus the ``read_source`` adjudicator sit alongside
        the original four KB tools, so the surface is 11. See
        docs/mcp-server.md.
        """
        tools = mcp_module.MCP_TOOLS
        assert len(tools) == 11
        names = {t["name"] for t in tools}
        assert names == {
            # KB tools
            "query_knowledge_base",
            "list_workspaces",
            "list_workspace_documents",
            "get_coding_standards",
            # Symbol-graph tools
            "find_callers",
            "find_importers",
            "find_implementers",
            "find_definitions",
            "find_symbol_by_name",
            "find_symbol_at_line",
            # Read-source adjudicator
            "read_source",
        }

    def test_tools_have_required_fields(self):
        """Test that each tool has name, description, and inputSchema."""
        for tool in mcp_module.MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_all_tool_handlers_exist(self):
        """Test that every defined tool has a handler."""
        for tool in mcp_module.MCP_TOOLS:
            assert tool["name"] in mcp_module.TOOL_HANDLERS


class TestFormatResults:
    """Test result formatting."""

    def test_format_empty_results(self):
        """Test formatting with no results."""
        result = mcp_module.format_results([])
        assert result == "No relevant documents found."

    def test_format_results_with_content(self):
        """Test formatting with results (snippet + match-score format)."""
        results = [
            {"repo": "my-repo", "type": "adr", "text": "Some content here", "rerank_score": 0.91},
            {"repo": "other-repo", "type": "readme", "text": "Other content", "score": 0.5},
        ]
        text = mcp_module.format_results(results, query="content")
        assert "[1] my-repo (adr)" in text
        assert "match 0.91" in text
        assert "Some content here" in text
        assert "[2] other-repo (readme)" in text
        assert "Other content" in text

    def test_format_results_caps_total_response_size(self):
        """The 60KB-for-one-question regression: 10 large chunks must be
        bounded by max_tokens, dropping lower-ranked results with an
        explicit omission notice — never dumping everything verbatim."""
        # 10 chunks of ~6KB each = ~60KB if dumped verbatim.
        big = "Widgets and gizmos. " * 300  # ~6KB
        results = [
            {"repo": f"repo{i}", "type": "doc", "text": big, "rerank_score": 1.0 - i * 0.05}
            for i in range(10)
        ]
        text = mcp_module.format_results(results, query="widgets", max_tokens=4000)
        # ~4 chars/token => 4000 tokens ~= 16KB. Allow slack for the
        # omission notice + separators, but it must be a fraction of 60KB.
        assert len(text) < 20000
        assert "omitted" in text
        # The top-ranked result survives intact (snippet, not dropped).
        assert "[1] repo0 (doc)" in text

    def test_format_results_per_result_snippet_windowing(self):
        """A single oversized chunk is windowed to a snippet, not dumped
        in full — bounded by the per-result snippet ceiling."""
        big = "Widgets everywhere. " * 1000  # ~20KB single chunk
        results = [{"repo": "r", "type": "doc", "text": big, "rerank_score": 0.8}]
        text = mcp_module.format_results(results, query="widgets", max_tokens=16000)
        assert len(text) < len(big)
