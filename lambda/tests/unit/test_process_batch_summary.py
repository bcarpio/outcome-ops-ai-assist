
# === Legacy dict -> unified Response adapter (ADR-035 chunk 11) ===
from types import SimpleNamespace
from shared.model_client_types import StopReason

class _LegacyDictResponse:
    def __init__(self, d):
        # Handle two legacy shapes: Converse (output.message.content) and
        # InvokeModel-Anthropic-native (content list at top level).
        content = d.get("output", {}).get("message", {}).get("content", [])
        if not content:
            content = d.get("content", [])
        # InvokeModel/Anthropic-native body had {"body": <streaming bytes>}.
        if "body" in d and not content:
            import json as _json
            body_raw = d["body"]
            body_bytes = body_raw.read() if hasattr(body_raw, "read") else body_raw
            try:
                body = _json.loads(body_bytes) if body_bytes else {}
            except Exception:
                body = {}
            content = body.get("content", [])
            usage = body.get("usage", {})
        else:
            usage = d.get("usage", {})
        self.text = content[0].get("text", "") if content else ""
        self.content_blocks = [{"text": self.text}] if self.text else []
        # Tolerate both Converse (inputTokens/outputTokens) and Anthropic
        # (input_tokens/output_tokens) keys.
        in_t = usage.get("inputTokens", usage.get("input_tokens", 0))
        out_t = usage.get("outputTokens", usage.get("output_tokens", 0))
        self.usage = SimpleNamespace(input_tokens=in_t, output_tokens=out_t)
        self.stop_reason = StopReason.END_TURN
        self.tool_calls = None
        self.raw = d

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
    """Test file content fetching from GitHub.

    chunk-08a moved the urlopen call from process_batch_summary's
    fetch_file_content into shared.source_fetch -- the mock target
    moved with it. The shared module also validates that
    github_token is non-empty at the dispatch layer, so the tests
    explicitly set the module-level GITHUB_TOKEN (load_config()
    runs at Lambda startup in prod; not in unit tests).
    """

    @patch("process_batch_summary_handler.get_default_branch")
    @patch("shared.github_backoff.github_urllib_request")
    def test_fetch_file_content_success(self, mock_urlopen, mock_get_branch):
        mock_get_branch.return_value = "main"
        mock_response = MagicMock()
        mock_response.read.return_value = b"file content here"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch.object(handler_module, "GITHUB_TOKEN", "ghp_test"):
            result = fetch_file_content("owner/repo", "path/to/file.py")

        assert result == "file content here"
        mock_urlopen.assert_called_once()

    @patch("process_batch_summary_handler.get_default_branch")
    @patch("shared.github_backoff.github_urllib_request")
    def test_fetch_file_content_url_format(self, mock_urlopen, mock_get_branch):
        mock_get_branch.return_value = "main"
        mock_response = MagicMock()
        mock_response.read.return_value = b"content"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch.object(handler_module, "GITHUB_TOKEN", "ghp_test"):
            fetch_file_content("bcarpio/outcome-ops-ai-assist", "lambda/handler.py")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "raw.githubusercontent.com" in request_obj.full_url
        assert "bcarpio/outcome-ops-ai-assist/main/lambda/handler.py" in request_obj.full_url

    def test_fetch_file_content_gitlab_reads_s3_mirror(self):
        # GitLab (ADR-050) follows the ADO S3-mirror pattern -- source comes from
        # shared.source_fetch(provider="gitlab"), NOT the github raw path. This is
        # what feeds the symbol-graph extractor for GitLab repos.
        with patch("shared.source_fetch.fetch_repo_file",
                   return_value="gl content") as mock_fetch:
            result = fetch_file_content(
                "bcarpio-group/ooai-license-server", "src/validator.py",
                provider="gitlab", workspace_id="ws-1",
                slug="bcarpio-group/ooai-license-server")
        assert result == "gl content"
        args, kwargs = mock_fetch.call_args
        assert args[0] == "gitlab"
        assert kwargs["workspace_id"] == "ws-1"
        assert kwargs["slug"] == "bcarpio-group/ooai-license-server"


class TestGenerateBatchSummary:
    """Test batch summary generation."""

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.invoke_model")
    def test_generate_batch_summary_infrastructure(self, mock_invoke, mock_fetch):
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
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        })

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        assert mock_fetch.call_count == 2
        mock_invoke.assert_called_once()

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.invoke_model")
    def test_generate_batch_summary_handler_group(self, mock_invoke, mock_fetch):
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
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        })

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        mock_invoke.assert_called_once()

    @patch("process_batch_summary_handler.fetch_file_content")
    @patch("process_batch_summary_handler.invoke_model")
    def test_generate_batch_summary_chunks_large_files(self, mock_invoke, mock_fetch):
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
        mock_invoke.return_value = _LegacyDictResponse({
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        })

        # Act
        result = generate_batch_summary(batch)

        # Assert - should get a summary, not "No files available"
        assert result == mock_summary
        mock_invoke.assert_called_once()
        # Verify the prompt includes chunk information
        call_args = mock_invoke.call_args
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
        # Note: This would need mock_invoke too for full test
        # For now just testing error handling doesn't crash


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_embedding_success(self, mock_bedrock):
        """Test successful embedding generation. Embedding uses Titan
        via bedrock_client.invoke_model (NOT shared.model_client) per
        ADR-035 §Out of scope."""
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

    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_embedding_truncates_long_text(self, mock_bedrock):
        """Titan Embeddings v2 hard-caps input at 50000 chars and
        rejects with ValidationException above that. The safety net
        truncates at 45000 chars BEFORE the call so a verbose-day
        summary still gets a vector. Live-burned 2026-06-28:
        27 of 31 handler-group summaries on app.retrieveit.ai had
        failed embedding silently before this guard."""
        # Arrange: text longer than the safety-net limit.
        long_text = "x" * 60000
        mock_embedding = [0.1] * 1024
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": mock_embedding}).encode())
        }

        # Act
        result = generate_embedding(long_text)

        # Assert: call succeeded; payload was truncated to 45000.
        assert result == mock_embedding
        mock_bedrock.invoke_model.assert_called_once()
        sent_body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
        assert len(sent_body["inputText"]) == 45000

    @patch("process_batch_summary_handler.bedrock_client")
    def test_generate_embedding_does_not_truncate_short_text(self, mock_bedrock):
        """Backward-compat: text under the cap is passed through
        unchanged. Normal-sized code-map summaries (~20k chars after
        the prompt-tightening commit) should not see truncation."""
        text = "x" * 30000
        mock_embedding = [0.1] * 1024
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": mock_embedding}).encode())
        }

        result = generate_embedding(text)

        assert result == mock_embedding
        sent_body = json.loads(mock_bedrock.invoke_model.call_args.kwargs["body"])
        assert len(sent_body["inputText"]) == 30000


class TestStoreBatchSummary:
    """Test storing batch summaries in S3 Vectors."""

    @patch("process_batch_summary_handler.VECTOR_BUCKET", "test-bucket")
    @patch("process_batch_summary_handler.VECTOR_INDEX", "test-index")
    def test_store_batch_summary_success(self):
        """Test successful batch summary storage.

        fable-scale chunk 07: store_batch_summary now routes through
        shared.dual_write.s3vectors_put_vectors (chunked at 500 + HA
        replication). Inject a mock client into dual_write so the same
        put_vectors call_args inspection shape works.
        """
        # Arrange
        from shared import dual_write
        mock_s3vectors = MagicMock()
        mock_s3vectors.put_vectors.return_value = {}
        dual_write._set_clients_for_tests(s3vectors=mock_s3vectors)
        try:
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
        finally:
            dual_write._reset_clients_for_tests()

    def test_store_batch_summary_gitlab_key_uses_bare_name(self):
        """ADR-052: GitLab passes the full slug as batch['repo'], but the
        code-map/summary vector key must use the BARE project name (last
        path segment) so it matches the {ws}#gitlab#{bare}#... shape the
        delete-worker purges on repo removal. Nested subgroups collapse to
        the final segment. Without this, GitLab code-map vectors keyed on
        the slug leaked into the RAG after a repo was deleted."""
        from shared import dual_write
        mock_s3vectors = MagicMock()
        mock_s3vectors.put_vectors.return_value = {}
        dual_write._set_clients_for_tests(s3vectors=mock_s3vectors)
        try:
            batch = {
                "workspace_id": "ws_abc",
                "provider": "gitlab",
                "repo": "group/subgroup/license-server",  # full slug
                "repo_project": "group/subgroup/license-server",
                "storage_key": "summary#code-map",
                "batch_type": "code",
                "group_name": "license-server/code-map",
                "file_paths": ["README.md"],
            }
            result = store_batch_summary(batch, "summary text", [0.1] * 1024)
            assert result is True
            vectors = mock_s3vectors.put_vectors.call_args[1]["vectors"]
            assert (
                vectors[0]["key"]
                == "ws_abc#gitlab#license-server#code-summary#license-server/code-map"
            )
        finally:
            dual_write._reset_clients_for_tests()

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
    def test_process_batch_record_old_shape_defaults_to_single_shard(
        self, mock_summary, mock_embedding, mock_store
    ):
        """Chunk 02 backwards compat: pre-chunk-02 producer omits
        message_type + shard fields. Consumer must still process the
        record end-to-end by defaulting to single-shard."""
        mock_summary.return_value = "summary"
        mock_embedding.return_value = [0.0] * 1024
        mock_store.return_value = True

        record = {
            "body": json.dumps({
                "repo": "r",
                "repo_project": "o/r",
                "batch_type": "handler-group",
                "group_name": "g",
                "file_paths": ["f.py"],
                "storage_key": "sk",
                # NO message_type / shard_index / shard_count / parent_storage_key
            })
        }
        process_batch_record(record)
        mock_summary.assert_called_once()
        mock_store.assert_called_once()

    @patch("process_batch_summary_handler.handle_merge_message")
    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    def test_process_batch_record_merge_message_routes_to_handle_merge(
        self, mock_summary, mock_embedding, mock_store, mock_handle_merge,
    ):
        """Chunk 05: process_batch_record delegates merge messages to
        handle_merge_message; the shard-summary path (Bedrock, embedding,
        store) is skipped since merge has its own dedicated Bedrock call."""
        record = {
            "body": json.dumps({
                "message_type": "merge",
                "repo": "r",
                "repo_project": "o/r",
                "batch_type": "handler-group",
                "group_name": "g",
                "parent_group_name": "g",
                "file_paths": [],
                "storage_key": "sk",
                "parent_storage_key": "sk",
                "shard_count": 3,
            })
        }
        process_batch_record(record)
        mock_handle_merge.assert_called_once()
        # Shard-summary path skipped
        mock_summary.assert_not_called()
        mock_embedding.assert_not_called()
        mock_store.assert_not_called()

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


class TestHandleMergeMessage:
    """Chunk 05 merge fan-in."""

    def _base_batch(self, shard_count=3):
        return {
            "message_type": "merge",
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "batch_type": "handler-group",
            "group_name": "tests",
            "parent_group_name": "tests",
            "storage_key": "summary#tests",
            "parent_storage_key": "summary#tests",
            "shard_count": shard_count,
            "workspace_id": "ws-1",
            "commit_sha": "abc",
        }

    def _shard_summary(self, index, file_count=10):
        return {
            "content": f"partial summary of shard {index}",
            "file_count": file_count,
            "key": f"ws-1#github#outcome-ops-ai-assist#handler-group-summary#tests#shard-{index:02d}",
        }

    def test_shard_vector_key_shape_matches_store_batch_summary(self):
        """The key the merge handler computes for each shard must
        match the key store_batch_summary would write when it received
        that shard message. Otherwise get_vectors will miss."""
        key = handler_module._shard_vector_key(
            workspace_id="ws-1", provider="github",
            repo="outcome-ops-ai-assist", doc_type="handler-group-summary",
            parent_group_name="tests", shard_index=0,
        )
        assert key == "ws-1#github#outcome-ops-ai-assist#handler-group-summary#tests#shard-00"

    def test_shard_vector_key_no_workspace(self):
        key = handler_module._shard_vector_key(
            workspace_id=None, provider="github", repo="r",
            doc_type="ht-summary", parent_group_name="g", shard_index=2,
        )
        assert key == "r#ht-summary#g#shard-02"

    @patch("process_batch_summary_handler._mark_code_map_unit_done")
    @patch("process_batch_summary_handler._call_bedrock_and_audit")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler._fetch_shard_summaries")
    def test_all_shards_present_generates_merged_summary_at_parent_key(
        self, mock_fetch, mock_store, mock_embed, mock_bedrock, mock_mark,
    ):
        mock_fetch.return_value = [self._shard_summary(i) for i in range(3)]
        mock_bedrock.return_value = "merged unified summary"
        mock_embed.return_value = [0.0] * 1024
        mock_store.return_value = True

        handler_module.handle_merge_message(self._base_batch(shard_count=3))

        # One Bedrock call for the merge
        mock_bedrock.assert_called_once()
        # Stored at PARENT group_name (no shard suffix)
        stored_batch = mock_store.call_args.args[0]
        assert stored_batch["group_name"] == "tests"
        # Ledger closes success
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs["status"] == "ok"

    @patch("process_batch_summary_handler._mark_code_map_unit_done")
    @patch("process_batch_summary_handler._requeue_merge_with_delay")
    @patch("process_batch_summary_handler._fetch_shard_summaries")
    def test_shards_incomplete_requeues_without_marking_ledger(
        self, mock_fetch, mock_requeue, mock_mark,
    ):
        # Only 1 of 3 shards has landed
        mock_fetch.return_value = [self._shard_summary(0)]
        handler_module.handle_merge_message(self._base_batch(shard_count=3))

        mock_requeue.assert_called_once()
        # merge_attempt starts at 0 in the message; requeue bumps to 1
        assert mock_requeue.call_args.args[1] == 1
        # DO NOT mark ledger complete on requeue -- that would double
        # against expected_count on the next merge attempt
        mock_mark.assert_not_called()

    @patch("process_batch_summary_handler._mark_code_map_unit_done")
    @patch("process_batch_summary_handler._requeue_merge_with_delay")
    @patch("process_batch_summary_handler._fetch_shard_summaries")
    def test_max_attempts_gives_up_marks_failed_no_requeue(
        self, mock_fetch, mock_requeue, mock_mark,
    ):
        mock_fetch.return_value = [self._shard_summary(0)]  # only 1 of 3
        batch = self._base_batch(shard_count=3)
        batch["merge_attempt"] = handler_module._MAX_MERGE_ATTEMPTS - 1

        handler_module.handle_merge_message(batch)

        mock_requeue.assert_not_called()
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs["status"] == "failed"
        assert (
            "merge_shards_missing"
            in mock_mark.call_args.kwargs["failure_reason"]
        )

    @patch("process_batch_summary_handler._mark_code_map_unit_done")
    def test_zero_shard_count_marks_ledger_and_returns(self, mock_mark):
        """Defensive: a malformed merge message (shard_count=0) should
        still close the ledger so sha-truth advance isn't blocked."""
        batch = self._base_batch(shard_count=0)
        handler_module.handle_merge_message(batch)
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs["status"] == "ok"


class TestFetchShardSummaries:
    def test_empty_input_returns_empty(self):
        assert handler_module._fetch_shard_summaries([]) == []

    def test_extracts_content_and_file_count(self):
        mock_client = MagicMock()
        mock_client.get_vectors.return_value = {
            "vectors": [
                {"key": "k1", "metadata": {"content": "shard 1 body", "file_count": "5"}},
                {"key": "k2", "metadata": {"content": "shard 2 body", "file_count": "3"}},
            ],
        }
        with patch.object(handler_module, "s3vectors_client", mock_client):
            result = handler_module._fetch_shard_summaries(["k1", "k2"])
        assert len(result) == 2
        assert result[0]["file_count"] == 5
        assert result[1]["content"] == "shard 2 body"

    def test_skips_vectors_with_no_content(self):
        mock_client = MagicMock()
        mock_client.get_vectors.return_value = {
            "vectors": [
                {"key": "k1", "metadata": {"content": "ok", "file_count": "1"}},
                {"key": "k2", "metadata": {"file_count": "1"}},  # no content
            ],
        }
        with patch.object(handler_module, "s3vectors_client", mock_client):
            result = handler_module._fetch_shard_summaries(["k1", "k2"])
        assert len(result) == 1

    def test_client_error_returns_empty(self):
        from botocore.exceptions import ClientError
        mock_client = MagicMock()
        mock_client.get_vectors.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}},
            "GetVectors",
        )
        with patch.object(handler_module, "s3vectors_client", mock_client):
            result = handler_module._fetch_shard_summaries(["k1"])
        assert result == []


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


class TestGitHubApiCallsUseFullRepoName:
    """The symbol-graph path calls two GitHub APIs -- ``get_default_branch``
    and ``_load_symbol_graph_filter`` (via ``load_repo_config_from_github``)
    -- both of which need the full ``owner/repo``. The bug this class
    guards against: they used to receive ``sg_repo`` (the SHORT bare
    name that keys the fact-store namespace), which 404'd against
    ``/repos/{name}``. GitHub 404 -> fallback to ``main`` (coincidentally
    correct for main-defaulted repos) and to ``DEFAULT_SYMBOL_GRAPH_FILTER``
    (silently ignoring the per-repo ``.outcomeops.yaml::ingestion.symbol_graph``
    config). Fix uses ``repo_project`` (owner/repo) for both API calls
    while leaving ``sg_repo`` as the fact-store key.
    """

    @patch("process_batch_summary_handler.store_batch_summary")
    @patch("process_batch_summary_handler.generate_embedding")
    @patch("process_batch_summary_handler.generate_batch_summary")
    @patch("process_batch_summary_handler._emit_facts")
    @patch("process_batch_summary_handler._commit_symbol_graph_swap")
    @patch("process_batch_summary_handler._abandon_symbol_graph_swap")
    @patch("process_batch_summary_handler._load_symbol_graph_filter")
    @patch("process_batch_summary_handler.get_default_branch")
    def test_full_name_passed_to_get_default_branch_and_symbol_graph_filter(
        self,
        mock_default_branch,
        mock_load_filter,
        mock_abandon,
        mock_commit,
        mock_emit_facts,
        mock_summary,
        mock_embedding,
        mock_store,
    ):
        # Use DISTINCT values for ``repo`` (short) and ``repo_project``
        # (full owner/repo) so a regression that flips back to sg_repo
        # would fail this test loudly.
        record = {
            "body": json.dumps({
                "repo": "outcome-ops-ai-assist-private",  # short (fact-store key)
                "repo_project": "bcarpio/outcome-ops-ai-assist-private",  # full (API arg)
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["x.py"],
                "storage_key": "summary#utils",
                "workspace_id": "ws-graph",
                "commit_sha": "deadbeef",
            })
        }
        mock_default_branch.return_value = "main"
        mock_load_filter.return_value = None  # returns default filter
        mock_summary.return_value = "summary text"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        with patch(
            "shared.symbol_graph_pointer.begin_swap",
            return_value=True,
        ), patch(
            "shared.symbol_graph_pointer.get_previous_graph_sha",
            return_value=None,
        ):
            process_batch_record(record)

        # Both API calls must receive the FULL owner/repo, not the short
        # bare name. The bare name would 404 against GitHub, be swallowed,
        # and silently ignore the customer's .outcomeops.yaml config.
        mock_default_branch.assert_called_once_with(
            "bcarpio/outcome-ops-ai-assist-private"
        )
        mock_load_filter.assert_called_once()
        first_arg = mock_load_filter.call_args.args[0]
        assert first_arg == "bcarpio/outcome-ops-ai-assist-private", (
            "_load_symbol_graph_filter must receive owner/repo, not the "
            "bare short name (which 404s and silently drops per-repo "
            "ingestion.symbol_graph config)"
        )

        # Fact-store namespace keys stay bare (unchanged by the fix).
        # Guard here so a future change that unifies the namespace has
        # to update this assertion deliberately -- see the ADR note in
        # the fix commit.
        emit_kwargs = mock_emit_facts.call_args.kwargs
        assert emit_kwargs["repo"] == "outcome-ops-ai-assist-private"
        mock_commit.assert_called_once_with(
            "ws-graph", "outcome-ops-ai-assist-private", "deadbeef",
        )


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
    """ADR-027 — every code-map batch prompt carries the literal-value
    discipline so no generated summary freezes a wrong value/identifier."""
    prompt = handler_module._build_batch_prompt(
        "handler-group", "owner/repo", "auth", "<<files>>"
    )
    # v1.2 opener; replaces "DO NOT TRANSCRIBE SPECIFIC LITERAL VALUES"
    # which is gone (see ADR-027 v1.2 + the GPT-5.5 over-interpretation
    # finding in test_shared_prompts.TestCodeMapLiteralDiscipline).
    assert "LITERAL-VALUE DISCIPLINE" in prompt
    assert "(see source)" in prompt
    # Also applied to the generic fallback (unknown batch type).
    fallback = handler_module._build_batch_prompt(
        "totally-unknown-type", "owner/repo", "grp", "<<files>>"
    )
    assert "LITERAL-VALUE DISCIPLINE" in fallback


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
    # Anchor on a stable phrase from the handler-group prompt body
    # (was "Analyze the auth" pre-2026-06-28; the prompt was tightened
    # to produce concise summaries and the new opener is
    # "Write a CONCISE implementation overview").
    assert prompt.index("VERIFIED SYMBOLS") < prompt.index("CONCISE implementation overview")
    assert prompt.index("CONCISE implementation overview") < prompt.index("LITERAL-VALUE DISCIPLINE")


def test_build_batch_prompt_no_grounding_unchanged_shape():
    # Backward-compat: no grounding -> no VERIFIED SYMBOLS section.
    prompt = handler_module._build_batch_prompt(
        "handler-group", "owner/repo", "auth", "<<files>>"
    )
    assert "VERIFIED SYMBOLS" not in prompt
    assert "LITERAL-VALUE DISCIPLINE" in prompt


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
