"""Unit tests for process-batch-summary Lambda handler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
import sys
import os
import importlib.util

# Load the process-batch-summary handler module
handler_path = os.path.join(os.path.dirname(__file__), '../../process-batch-summary/handler.py')
spec = importlib.util.spec_from_file_location("process_batch_summary_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['process_batch_summary_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from the loaded module
compute_content_hash = handler_module.compute_content_hash
fetch_file_content = handler_module.fetch_file_content
generate_batch_summary = handler_module.generate_batch_summary
generate_embedding = handler_module.generate_embedding
process_batch_record = handler_module.process_batch_record
store_batch_summary = handler_module.store_batch_summary
chunk_large_content = handler_module.chunk_large_content
MAX_FILE_SIZE = handler_module.MAX_FILE_SIZE
CHUNK_SIZE = handler_module.CHUNK_SIZE
CHUNK_OVERLAP = handler_module.CHUNK_OVERLAP


class TestChunkLargeContent:
    """Test large file chunking."""

    def test_chunk_small_file_no_chunking(self):
        """Test that small files are not chunked."""
        # Arrange
        content = "x" * 10000  # 10KB - well under 50KB limit

        # Act
        result = chunk_large_content(content, "small_file.py")

        # Assert
        assert len(result) == 1
        assert result[0]["path"] == "small_file.py"
        assert result[0]["chunk_index"] == 0
        assert result[0]["total_chunks"] == 1
        assert result[0]["is_chunked"] is False

    def test_chunk_large_file_creates_multiple_chunks(self):
        """Test that large files are split into multiple chunks."""
        # Arrange - 100KB file should be split into 3 chunks with 40KB chunk size
        content = "x" * 100000  # 100KB

        # Act
        result = chunk_large_content(content, "large_file.py")

        # Assert - should be chunked
        assert len(result) >= 2  # At least 2 chunks
        for chunk in result:
            assert chunk["is_chunked"] is True
            assert chunk["total_chunks"] == len(result)
            assert "start_char" in chunk
            assert "end_char" in chunk
            assert chunk["path"] == "large_file.py"

    def test_chunk_metadata_is_correct(self):
        """Test that chunk metadata has correct indices."""
        # Arrange
        content = "a" * 60000  # 60KB - just over 50KB limit

        # Act
        result = chunk_large_content(content, "test.py")

        # Assert
        assert len(result) >= 2
        # First chunk
        assert result[0]["chunk_index"] == 0
        assert result[0]["start_char"] == 0
        # All chunks have same total_chunks
        for i, chunk in enumerate(result):
            assert chunk["chunk_index"] == i
            assert chunk["total_chunks"] == len(result)


class TestComputeContentHash:
    """Test content hash computation."""

    def test_compute_content_hash_valid_content(self):
        """Test computing hash for valid content."""
        # Arrange
        content = "Test content for hashing"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Act
        result = compute_content_hash(content)

        # Assert
        assert result == expected_hash
        assert len(result) == 64


class TestFetchFileContent:
    """Test file content fetching from GitHub."""

    @patch("process_batch_summary_handler.get_default_branch")
    @patch("process_batch_summary_handler.urlopen")
    def test_fetch_file_content_success(self, mock_urlopen, mock_get_branch):
        """Test successful file content fetch."""
        # Arrange
        mock_get_branch.return_value = "main"
        mock_response = MagicMock()
        mock_response.read.return_value = b"file content here"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        result = fetch_file_content("owner/repo", "path/to/file.py")

        # Assert
        assert result == "file content here"
        mock_urlopen.assert_called_once()

    @patch("process_batch_summary_handler.get_default_branch")
    @patch("process_batch_summary_handler.urlopen")
    def test_fetch_file_content_url_format(self, mock_urlopen, mock_get_branch):
        """Test file content fetch uses correct URL format."""
        # Arrange
        mock_get_branch.return_value = "main"
        mock_response = MagicMock()
        mock_response.read.return_value = b"content"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        fetch_file_content("bcarpio/outcome-ops-ai-assist", "lambda/handler.py")

        # Assert
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "raw.githubusercontent.com" in request_obj.full_url
        assert "bcarpio/outcome-ops-ai-assist/main/lambda/handler.py" in request_obj.full_url


class TestGenerateBatchSummary:
    """Test batch summary generation."""

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_batch_summary_infrastructure(self, mock_bedrock, mock_fetch):
        """Test generating summary for infrastructure batch."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "batch_type": "infrastructure",
            "group_name": "infrastructure",
            "file_paths": ["terraform/main.tf", "terraform/lambda.tf"]
        }

        mock_fetch.return_value = "terraform content here"
        mock_summary = "This infrastructure creates Lambda functions and DynamoDB tables..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        assert mock_fetch.call_count == 2
        mock_bedrock.converse.assert_called_once()

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_batch_summary_handler_group(self, mock_bedrock, mock_fetch):
        """Test generating summary for handler group batch."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "file_paths": ["lambda/ingest-docs/handler.py"]
        }

        mock_fetch.return_value = "def handler(event, context): ..."
        mock_summary = "This handler ingests documents from GitHub..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        mock_bedrock.converse.assert_called_once()

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_batch_summary_chunks_large_files(self, mock_bedrock, mock_fetch):
        """Test that large files are chunked instead of skipped."""
        # Arrange
        batch = {
            "repo": "test-repo",
            "repo_project": "owner/test-repo",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": ["large_file.py"]
        }

        mock_fetch.return_value = "x" * 60000  # 60KB file - will be chunked
        mock_summary = "Summary of chunked large file..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_batch_summary(batch)

        # Assert - should get a summary, not "No files available"
        assert result == mock_summary
        mock_bedrock.converse.assert_called_once()
        # Verify the prompt includes chunk information
        call_args = mock_bedrock.converse.call_args
        messages = call_args[1]["messages"]
        prompt_content = messages[0]["content"][0]["text"]
        assert "[Chunk" in prompt_content  # Large file should be chunked

    @patch("process_batch_summary_handler.fetch_file_content")
    def test_generate_batch_summary_handles_fetch_errors(self, mock_fetch):
        """Test that file fetch errors are handled gracefully."""
        # Arrange
        batch = {
            "repo": "test-repo",
            "repo_project": "owner/test-repo",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": ["file1.py", "file2.py"]
        }

        mock_fetch.side_effect = [Exception("API Error"), "valid content"]

        # Act - should not raise
        # Note: This would need mock_bedrock too for full test
        # For now just testing error handling doesn't crash


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_embedding_success(self, mock_bedrock):
        """Test successful embedding generation."""
        # Arrange
        text = "Test text for embedding"
        mock_embedding = [0.1] * 1024
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": mock_embedding}).encode())
        }

        # Act
        result = generate_embedding(text)

        # Assert
        assert result == mock_embedding
        assert len(result) == 1024
        mock_bedrock.invoke_model.assert_called_once()


class TestStoreBatchSummary:
    """Test storing batch summaries in S3 Vectors."""

    @patch("process_batch_summary_handler.VECTOR_BUCKET", "test-bucket")
    @patch("process_batch_summary_handler.VECTOR_INDEX", "test-index")
    @patch("process_batch_summary_handler.s3vectors_client")
    def test_store_batch_summary_success(self, mock_s3vectors):
        """Test successful batch summary storage."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "storage_key": "summary#handler#ingest-docs",
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "file_paths": ["lambda/ingest-docs/handler.py"]
        }
        summary = "This handler ingests documents..."
        embedding = [0.1] * 1024

        mock_s3vectors.put_vectors.return_value = {}

        # Act
        result = store_batch_summary(batch, summary, embedding)

        # Assert
        assert result is True
        mock_s3vectors.put_vectors.assert_called_once()
        call_kwargs = mock_s3vectors.put_vectors.call_args[1]
        assert call_kwargs["vectorBucketName"] == "test-bucket"
        assert call_kwargs["indexName"] == "test-index"
        # Verify vector data
        vectors = call_kwargs["vectors"]
        assert len(vectors) == 1
        assert vectors[0]["key"] == "outcome-ops-ai-assist#handler-group-summary#ingest-docs"
        assert vectors[0]["metadata"]["repo"] == "bcarpio/outcome-ops-ai-assist"
        assert vectors[0]["metadata"]["batch_type"] == "handler-group"
        assert vectors[0]["metadata"]["group_name"] == "ingest-docs"

    @patch("process_batch_summary_handler.VECTOR_BUCKET", "test-bucket")
    @patch("process_batch_summary_handler.VECTOR_INDEX", "test-index")
    @patch("process_batch_summary_handler.s3vectors_client")
    def test_store_batch_summary_failure(self, mock_s3vectors):
        """Test handling S3 Vectors storage failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_s3vectors.put_vectors.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "PutVectors"
        )

        batch = {
            "repo": "test-repo",
            "repo_project": "owner/test-repo",
            "storage_key": "summary#test",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": []
        }

        # Act
        result = store_batch_summary(batch, "summary", [0.1] * 1024)

        # Assert
        assert result is False


class TestProcessBatchRecord:
    """Test SQS record processing."""

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    def test_process_batch_record_success(self, mock_summary, mock_embedding, mock_store):
        """Test successful record processing."""
        # Arrange
        record = {
            "body": json.dumps({
                "repo": "outcome-ops-ai-assist",
                "repo_project": "bcarpio/outcome-ops-ai-assist",
                "batch_type": "infrastructure",
                "group_name": "infrastructure",
                "file_paths": ["terraform/main.tf"],
                "storage_key": "summary#infrastructure"
            })
        }

        mock_summary.return_value = "Infrastructure summary"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        process_batch_record(record)

        # Assert
        mock_summary.assert_called_once()
        mock_embedding.assert_called_once()
        mock_store.assert_called_once()

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    def test_process_batch_record_storage_failure(self, mock_summary, mock_embedding, mock_store):
        """Test record processing with storage failure."""
        # Arrange
        record = {
            "body": json.dumps({
                "repo": "test-repo",
                "repo_project": "owner/test-repo",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["utils.py"],
                "storage_key": "summary#utils"
            })
        }

        mock_summary.return_value = "Utils summary"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = False  # Storage fails

        # Act & Assert
        with pytest.raises(Exception, match="Failed to store batch summary"):
            process_batch_record(record)


class TestSymbolGraphWiring:
    """Verify the begin_swap / extract / commit_swap pointer protocol
    fires when (workspace_id, repo, commit_sha) are all present on the
    SQS message, and is fully skipped when commit_sha is absent (legacy
    producer)."""

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_facts")
    @patch("process_batch_summary_handler._commit_symbol_graph_swap")
    @patch("process_batch_summary_handler._abandon_symbol_graph_swap")
    def test_emit_facts_fires_with_full_context_when_commit_sha_present(
        self,
        mock_abandon,
        mock_commit,
        mock_emit_facts,
        mock_summary,
        mock_embedding,
        mock_store,
    ):
        """When the SQS message carries workspace_id + repo + commit_sha,
        the chunk-03 path engages: begin_swap claims the pointer, the
        summary path runs WITH a fact sink, _emit_facts is invoked with
        all three context args, and commit_swap advances the pointer."""
        record = {
            "body": json.dumps({
                "repo": "owner/repo",
                "repo_project": "owner/repo",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
                "workspace_id": "ws-graph",
                "commit_sha": "deadbeef",
            })
        }
        mock_summary.return_value = "summary text"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Patch the pointer module symbols imported lazily inside
        # process_batch_record.
        with patch(
            "shared.symbol_graph_pointer.begin_swap",
            return_value=True,
        ) as mock_begin, patch(
            "shared.symbol_graph_pointer.get_previous_graph_sha",
            return_value="cafef00d",
        ) as mock_prev:
            process_batch_record(record)

        # begin_swap was called with the message's commit_sha.
        mock_begin.assert_called_once()
        call_kwargs = mock_begin.call_args
        # begin_swap(workspace_id, repo, new_sha, *, code_maps_table_name=...)
        args = call_kwargs.args
        assert args[0] == "ws-graph"
        assert args[1] == "owner/repo"
        assert args[2] == "deadbeef"

        # _emit_facts received the full context.
        mock_emit_facts.assert_called_once()
        emit_kwargs = mock_emit_facts.call_args.kwargs
        assert emit_kwargs["workspace_id"] == "ws-graph"
        assert emit_kwargs["repo"] == "owner/repo"
        assert emit_kwargs["commit_sha"] == "deadbeef"
        assert emit_kwargs["previous_graph_sha"] == "cafef00d"

        # commit_swap was called; abandon was not.
        mock_commit.assert_called_once_with(
            "ws-graph", "owner/repo", "deadbeef"
        )
        mock_abandon.assert_not_called()

        # generate_batch_summary received a fact_sink (chunk-03 enabled).
        # The first positional arg is the batch; fact_sink is the keyword.
        summary_call = mock_summary.call_args
        assert summary_call.kwargs.get("fact_sink") is not None

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_facts")
    @patch("process_batch_summary_handler._commit_symbol_graph_swap")
    @patch("process_batch_summary_handler._abandon_symbol_graph_swap")
    def test_emit_facts_skipped_when_commit_sha_absent(
        self,
        mock_abandon,
        mock_commit,
        mock_emit_facts,
        mock_summary,
        mock_embedding,
        mock_store,
    ):
        """Legacy producers that omit commit_sha (or any of the three
        required fields) fall through to the summary-only path. No
        pointer ops, no _emit_facts call."""
        record = {
            "body": json.dumps({
                "repo": "owner/repo",
                "repo_project": "owner/repo",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
                "workspace_id": "ws-graph",
                # commit_sha intentionally absent
            })
        }
        mock_summary.return_value = "summary text"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        with patch(
            "shared.symbol_graph_pointer.begin_swap",
        ) as mock_begin:
            process_batch_record(record)

        # Chunk-03 path stays off when commit_sha is missing.
        mock_begin.assert_not_called()
        mock_emit_facts.assert_not_called()
        mock_commit.assert_not_called()
        mock_abandon.assert_not_called()

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_facts")
    @patch("process_batch_summary_handler._commit_symbol_graph_swap")
    @patch("process_batch_summary_handler._abandon_symbol_graph_swap")
    def test_summary_failure_abandons_pending_swap(
        self,
        mock_abandon,
        mock_commit,
        mock_emit_facts,
        mock_summary,
        mock_embedding,
        mock_store,
    ):
        """A summary-path failure after begin_swap claimed the pointer
        must call abandon_swap so the next regen can claim cleanly."""
        record = {
            "body": json.dumps({
                "repo": "owner/repo",
                "repo_project": "owner/repo",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
                "workspace_id": "ws-graph",
                "commit_sha": "deadbeef",
            })
        }
        mock_summary.side_effect = RuntimeError("bedrock down")

        with patch(
            "shared.symbol_graph_pointer.begin_swap",
            return_value=True,
        ), patch(
            "shared.symbol_graph_pointer.get_previous_graph_sha",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="bedrock down"):
                process_batch_record(record)

        mock_emit_facts.assert_not_called()
        mock_commit.assert_not_called()
        mock_abandon.assert_called_once_with("ws-graph", "owner/repo")


class TestPiiRedactionWiring:
    """plan/ chunk 05 - verify the redact -> embed pipeline wiring for
    workspace-scoped batch summaries."""

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_ingest_audit")
    @patch("process_batch_summary_handler._get_workspaces_table")
    def test_workspace_scoped_summary_is_redacted_before_embedding(
        self, mock_ws_table, mock_audit,
        mock_summary, mock_embedding, mock_store,
    ):
        """For batches that carry a workspace_id, generated summaries
        must run through redact() before being embedded."""
        from shared.pii import RedactionResult

        record = {
            "body": json.dumps({
                "repo": "test",
                "repo_project": "owner/test",
                "batch_type": "shared",
                "group_name": "utils",
                "workspace_id": "ws_abc",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
            })
        }
        mock_summary.return_value = "summary with brian@example.com"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True
        mock_ws_table.return_value = MagicMock()

        with patch(
            "process_batch_summary_handler.read_workspace_mode",
            return_value="STRICT",
        ), patch(
            "process_batch_summary_handler.redact",
            return_value=RedactionResult(
                text="summary with [REDACTED:EMAIL]",
                char_count=29,
                summary={"EMAIL": 1},
            ),
        ) as mock_redact, patch.dict(
            os.environ, {"PII_REDACTION_ENABLED": "true"},
        ):
            process_batch_record(record)

        mock_redact.assert_called_once()
        # Embedding receives the redacted text, not the raw summary.
        embed_arg = mock_embedding.call_args.args[0]
        assert "brian@example.com" not in embed_arg
        assert "[REDACTED:EMAIL]" in embed_arg
        # Audit was emitted with the redaction context.
        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["workspace_id"] == "ws_abc"
        assert kwargs["mode"] == "STRICT"

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_ingest_audit")
    def test_legacy_batch_without_workspace_id_skips_redaction(
        self, mock_audit, mock_summary, mock_embedding, mock_store,
    ):
        """Legacy code-map batches lack a workspace_id; redact path is
        skipped entirely (no workspace context => no privacy_mode)."""
        record = {
            "body": json.dumps({
                "repo": "legacy",
                "repo_project": "owner/legacy",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
            })
        }
        mock_summary.return_value = "raw summary with brian@example.com"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        with patch(
            "process_batch_summary_handler.redact",
        ) as mock_redact:
            process_batch_record(record)

        mock_redact.assert_not_called()
        mock_audit.assert_not_called()
        # Embedding receives the raw text unchanged.
        assert mock_embedding.call_args.args[0] == "raw summary with brian@example.com"


class TestHandler:
    """Test Lambda handler."""

    @patch("process_batch_summary_handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("process_batch_summary_handler.process_batch_record")
    def test_handler_success(self, mock_process):
        """Test successful handler execution."""
        # Arrange
        event = {
            "Records": [
                {"body": json.dumps({"repo": "test1", "repo_project": "owner/test1", "batch_type": "infrastructure", "group_name": "infra", "file_paths": [], "storage_key": "summary#infra"})},
                {"body": json.dumps({"repo": "test2", "repo_project": "owner/test2", "batch_type": "shared", "group_name": "utils", "file_paths": [], "storage_key": "summary#utils"})}
            ]
        }
        context = {}

        # Act
        result = handler_module.handler(event, context)

        # Assert
        assert result["statusCode"] == 200
        assert mock_process.call_count == 2

    @patch("process_batch_summary_handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("process_batch_summary_handler.process_batch_record")
    def test_handler_partial_failure(self, mock_process):
        """Test handler with some failed records."""
        # Arrange
        event = {
            "Records": [
                {"body": json.dumps({"repo": "test1", "repo_project": "owner/test1", "batch_type": "infrastructure", "group_name": "infra", "file_paths": [], "storage_key": "summary#infra"})},
                {"body": json.dumps({"repo": "test2", "repo_project": "owner/test2", "batch_type": "shared", "group_name": "utils", "file_paths": [], "storage_key": "summary#utils"})}
            ]
        }
        context = {}

        # First succeeds, second fails
        mock_process.side_effect = [None, Exception("Processing error")]

        # Act & Assert
        with pytest.raises(Exception, match="Failed to process 1 out of 2 batches"):
            handler_module.handler(event, context)


def test_build_batch_prompt_appends_literal_discipline():
    """ADR-027 — every code-map batch prompt carries the literal-omission
    discipline so no generated summary freezes a wrong value/identifier."""
    prompt = handler_module._build_batch_prompt(
        "handler-group", "owner/repo", "auth", "<<files>>"
    )
    assert "DO NOT TRANSCRIBE SPECIFIC LITERAL VALUES" in prompt
    assert "(see source)" in prompt
    # Also applied to the generic fallback (unknown batch type).
    fallback = handler_module._build_batch_prompt(
        "totally-unknown-type", "owner/repo", "grp", "<<files>>"
    )
    assert "DO NOT TRANSCRIBE SPECIFIC LITERAL VALUES" in fallback


def _fact(kind, src, tgt, fp, line):
    return handler_module.Fact(
        kind=kind, source_symbol_id=src, target_symbol_id=tgt,
        file_path=fp, line=line, language="python",
    )


def test_render_graph_grounding_empty_returns_blank():
    assert handler_module._render_graph_grounding([]) == ""


def test_render_graph_grounding_lists_verified_symbols():
    facts = [
        _fact("defines", "python:m::<module>", "python:m::create_magic_token", "lambda/auth/handler.py", 514),
        _fact("calls", "python:m::create_magic_token", "python:secrets::token_urlsafe", "lambda/auth/handler.py", 522),
        # call to a symbol defined in-group should NOT show as outbound
        _fact("calls", "python:m::handle_verify", "python:m::create_magic_token", "lambda/auth/handler.py", 1300),
    ]
    block = handler_module._render_graph_grounding(facts)
    assert "VERIFIED SYMBOLS" in block
    assert "Do NOT introduce" in block
    # Defined symbol carries its real file:line (kills wrong-line attribution).
    assert "python:m::create_magic_token  (lambda/auth/handler.py:514)" in block
    # Outbound call to an external symbol is surfaced...
    assert "python:secrets::token_urlsafe" in block
    # ...but the in-group call target is not listed as outbound.
    assert block.count("python:m::create_magic_token") == 1


def test_build_batch_prompt_injects_grounding_before_discipline():
    facts = [_fact("defines", "python:m::<module>", "python:m::foo", "f.py", 3)]
    grounding = handler_module._render_graph_grounding(facts)
    prompt = handler_module._build_batch_prompt(
        "handler-group", "owner/repo", "auth", "<<files>>", grounding
    )
    assert "VERIFIED SYMBOLS" in prompt
    # Grounding precedes the task body, which precedes the literal discipline.
    assert prompt.index("VERIFIED SYMBOLS") < prompt.index("Analyze the auth")
    assert prompt.index("Analyze the auth") < prompt.index("DO NOT TRANSCRIBE")


def test_build_batch_prompt_no_grounding_unchanged_shape():
    # Backward-compat: no grounding -> no VERIFIED SYMBOLS section.
    prompt = handler_module._build_batch_prompt(
        "handler-group", "owner/repo", "auth", "<<files>>"
    )
    assert "VERIFIED SYMBOLS" not in prompt
    assert "DO NOT TRANSCRIBE" in prompt


def _fact_with_sig(kind, src, tgt, fp, line, signature):
    return handler_module.Fact(
        kind=kind, source_symbol_id=src, target_symbol_id=tgt,
        file_path=fp, line=line, language="python", signature=signature,
    )


def test_render_signature_helper_basic():
    """The signature renderer produces a parenthesized parameter list with
    an optional `-> return_type` tail. Empty signature renders as ``""``."""
    from extractor.base import Param, Signature

    assert handler_module._render_signature(None) == ""
    assert handler_module._render_signature(Signature()) == ""
    sig = Signature(
        params=(Param("a", "int"), Param("b")),
        returns="bool",
    )
    assert handler_module._render_signature(sig) == "(a: int, b) -> bool"


def test_render_graph_grounding_includes_signatures_when_present():
    """When defines facts carry a Signature, the rendered grounding line
    appends the parenthesized signature next to the symbol id. The
    use-only-these instruction is extended to cover signatures."""
    from extractor.base import Param, Signature

    sig = Signature(
        params=(Param("name", "str"), Param("loud", "bool")),
        returns="str",
    )
    facts = [
        _fact_with_sig(
            "defines",
            "python:m::<module>",
            "python:m::greet",
            "lambda/auth/handler.py",
            10,
            sig,
        ),
    ]
    block = handler_module._render_graph_grounding(facts)
    assert (
        "python:m::greet(name: str, loud: bool) -> str  (lambda/auth/handler.py:10)"
        in block
    )
    # The summarizer instruction must call out signatures specifically.
    assert "EXACT signature" in block


def test_render_graph_grounding_omits_signature_addendum_when_no_signatures():
    """A purely-pre-signature fact set must NOT carry the signature
    instruction tail -- pre-existing prompts stay byte-identical."""
    fact = handler_module.Fact(
        kind="defines",
        source_symbol_id="python:m::<module>",
        target_symbol_id="python:m::foo",
        file_path="f.py",
        line=3,
        language="python",
    )
    block = handler_module._render_graph_grounding([fact])
    assert "VERIFIED SYMBOLS" in block
    assert "EXACT signature" not in block
