# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for process-repo-summary Lambda handler.

Tests the repository summary generation and storage functions.
"""

import json
import os
import sys
import pytest
import importlib.util
from unittest.mock import patch, MagicMock
from types import ModuleType
from botocore.exceptions import ClientError


# Load handler using importlib to avoid module name collision
def load_handler_module():
    """Load the process-repo-summary handler with proper mocking."""
    handler_path = os.path.join(
        os.path.dirname(__file__),
        '../../process-repo-summary/handler.py'
    )

    # Create mock clients
    mock_dynamodb = MagicMock()
    mock_bedrock = MagicMock()
    mock_s3 = MagicMock()
    mock_sqs = MagicMock()
    mock_ssm = MagicMock()
    mock_s3vectors = MagicMock()

    def get_mock_client(service, **kwargs):
        clients = {
            'dynamodb': mock_dynamodb,
            'bedrock-runtime': mock_bedrock,
            's3': mock_s3,
            'sqs': mock_sqs,
            'ssm': mock_ssm,
            's3vectors': mock_s3vectors,
        }
        return clients.get(service, MagicMock())

    # Mock boto3
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = get_mock_client

    # Temporarily modify sys.modules
    original_boto3 = sys.modules.get('boto3')
    sys.modules['boto3'] = mock_boto3

    try:
        spec = importlib.util.spec_from_file_location(
            "process_repo_summary_handler",
            handler_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore original boto3 if it existed
        if original_boto3 is not None:
            sys.modules['boto3'] = original_boto3


# Load the handler module
repo_summary_handler = load_handler_module()


@pytest.fixture(autouse=True)
def _set_kb_bucket(monkeypatch):
    """The arch-summary upload now routes through
    shared.dual_write.s3_put_object, which validates a non-None bucket
    (the old direct s3_client.put_object mock tolerated None). Give the
    handler a bucket name so the upload path runs, mirroring deployed env."""
    monkeypatch.setattr(repo_summary_handler, "KB_BUCKET", "test-kb-bucket", raising=False)


class TestComputeContentHash:
    """Test content hash computation."""

    def test_compute_hash_returns_string(self):
        """Test: compute_content_hash returns a string."""
        result = repo_summary_handler.compute_content_hash("test content")
        assert isinstance(result, str)

    def test_compute_hash_is_deterministic(self):
        """Test: Same content produces same hash."""
        content = "test content"
        hash1 = repo_summary_handler.compute_content_hash(content)
        hash2 = repo_summary_handler.compute_content_hash(content)
        assert hash1 == hash2

    def test_compute_hash_different_content(self):
        """Test: Different content produces different hash."""
        hash1 = repo_summary_handler.compute_content_hash("content 1")
        hash2 = repo_summary_handler.compute_content_hash("content 2")
        assert hash1 != hash2

    def test_compute_hash_length(self):
        """Test: Hash is SHA-256 length (64 hex chars)."""
        result = repo_summary_handler.compute_content_hash("test")
        assert len(result) == 64


class TestRetryBedrockCall:
    """Test Bedrock retry logic."""

    def test_successful_call_no_retry(self):
        """Test: Successful call returns immediately."""
        mock_func = MagicMock(return_value="success")

        result = repo_summary_handler.retry_bedrock_call(mock_func, max_retries=3)

        assert result == "success"
        assert mock_func.call_count == 1

    def test_returns_result_from_function(self):
        """Test: Returns the result from the wrapped function."""
        expected = {"key": "value", "nested": {"data": [1, 2, 3]}}
        mock_func = MagicMock(return_value=expected)

        result = repo_summary_handler.retry_bedrock_call(mock_func)

        assert result == expected


class TestHandler:
    """Test main Lambda handler."""

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_handler_processes_record(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config
    ):
        """Test: Handler processes SQS record successfully."""
        mock_summary.return_value = "Test summary"
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["file1.py", "file2.py"],
                    "code_units": []
                })
            }]
        }

        result = repo_summary_handler.handler(event, None)

        assert result["batchItemFailures"] == []
        mock_summary.assert_called_once()
        mock_embed.assert_called_once()
        mock_store.assert_called_once()

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_arch_summary_put_object_sets_repo_metadata(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config,
    ):
        """The S3 PutObject for architectural-summary.txt must carry the
        github_repo_name / source / github_repo_type metadata so the
        process-documents re-ingest path tags the resulting vector with
        the org-prefixed repo identifier instead of the bare repo name.
        """
        mock_summary.return_value = "Test architectural summary"
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "app.retrieveit.ai",
                    "repo_project": "bcarpio/app.retrieveit.ai",
                    "repo_type": "application",
                    "all_files": ["file1.py"],
                    "code_units": [],
                    "workspace_id": "ws_engineering",
                }),
            }]
        }

        repo_summary_handler.handler(event, None)

        # Exactly one put_object call for the architectural summary
        # — the call must carry the Metadata kwarg with the prefixed
        # repo identifier.
        assert mock_s3.put_object.called
        put_kwargs = mock_s3.put_object.call_args.kwargs
        assert "Metadata" in put_kwargs
        meta = put_kwargs["Metadata"]
        assert meta["github_repo_name"] == "bcarpio/app.retrieveit.ai"
        assert meta["source"] == "github"
        assert meta["github_repo_type"] == "application"
        assert meta["workspace_id"] == "ws_engineering"

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    def test_handler_returns_failures(self, mock_summary, mock_config):
        """Test: Handler returns failed message IDs."""
        mock_summary.side_effect = Exception("Generation failed")

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["file1.py"],
                    "code_units": []
                })
            }]
        }

        result = repo_summary_handler.handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-1"

    @patch.object(repo_summary_handler, 'load_config')
    def test_handler_empty_records(self, mock_config):
        """Test: Handler handles empty records."""
        event = {"Records": []}

        result = repo_summary_handler.handler(event, None)

        assert result["batchItemFailures"] == []



# ============================================================================
# Code-map orphan cleanup emit
# ============================================================================


class TestExpectedCodeMapVectorKeys:
    """The deterministic key computation must match what
    process-batch-summary actually writes
    (process-batch-summary/handler.py:929-938).

    Critical detail: process-batch-summary appends '-summary' to
    batch_type. Backends return raw batch_types (handler-group,
    csharp-project, source-module, components, ...). The helper must
    apply the same suffix or expected_keys will never match stored
    keys and orphan cleanup will wipe everything as 'orphans'.
    """

    def test_workspace_keys_include_workspace_prefix_and_suffix(self):
        # Backends return raw batch_types WITHOUT -summary suffix; the
        # helper must append it to match stored key shape.
        units = [
            {"batch_type": "handler-group", "group_name": "chat"},
            {"batch_type": "handler-group", "group_name": "github-sync"},
            {"batch_type": "docs", "group_name": "documentation"},
        ]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws_abc"
        )
        assert keys == [
            "ws_abc#github#myrepo#handler-group-summary#chat",
            "ws_abc#github#myrepo#handler-group-summary#github-sync",
            "ws_abc#github#myrepo#docs-summary#documentation",
            "ws_abc#github#myrepo#code-map#myrepo/code-map",
        ]

    def test_no_workspace_uses_short_key_shape(self):
        """Mirrors the workspace_id-less branch in
        process-batch-summary/handler.py:938."""
        units = [{"batch_type": "handler-group", "group_name": "chat"}]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", None
        )
        assert keys == [
            "myrepo#handler-group-summary#chat",
            "myrepo#code-map#myrepo/code-map",
        ]

    def test_gitlab_slug_collapses_to_bare_name(self):
        # ADR-052: GitLab passes the full slug as repo, but the writer keys
        # on the BARE project name -- so the expected-key set (which the
        # orphan sweep compares against) must too, or it would treat every
        # real GitLab summary as an orphan.
        units = [{"batch_type": "handler-group", "group_name": "chat"}]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "group/subgroup/myrepo", "ws_abc", provider="gitlab"
        )
        assert keys == [
            "ws_abc#gitlab#myrepo#handler-group-summary#chat",
            "ws_abc#gitlab#myrepo#code-map#group/subgroup/myrepo/code-map",
        ]

    def test_empty_units_returns_empty(self):
        """Empty units MUST return [] -- the producer refuses to queue a
        sweep with empty expected_keys ('would wipe everything'). If the
        code-map key were appended unconditionally, a discovery failure
        would produce a one-key retain-list and the sweep would delete
        every unit summary for the repo."""
        keys = repo_summary_handler._expected_code_map_vector_keys(
            [], "myrepo", "ws"
        )
        assert keys == []

    def test_architectural_code_map_key_is_retained(self):
        """Regression for the 2026-07-19 code-map-eaten-by-sweep bug: the
        architectural code-map vector ({ws}#{prov}#{bare}#code-map#...)
        sits under the swept prefix and is not a _chunk_ per-doc key, but
        expected_keys never listed it -- so every regen's sweep deleted
        the code-map written moments earlier in the same run (live-hit:
        both regens on 2026-07-19, 'Found 1 orphan' each, ~20s after
        'Stored code map embedding'). No repo had a bare-keyed code-map
        at rest; only the sweep-dodging legacy slug-keyed twins survived."""
        units = [{"batch_type": "handler-group", "group_name": "chat"}]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws_abc"
        )
        assert "ws_abc#github#myrepo#code-map#myrepo/code-map" in keys

    def test_multi_backend_batch_types_get_summary_suffix(self):
        """Different backends produce different raw batch_types - all
        must get the same -summary suffix treatment, matching what
        process-batch-summary writes for any backend."""
        units = [
            {"batch_type": "handler-group", "group_name": "chat"},          # python
            {"batch_type": "csharp-project", "group_name": "Web"},          # csharp
            {"batch_type": "csharp-services", "group_name": "Auth"},        # csharp
            {"batch_type": "source-module", "group_name": "api"},           # java
            {"batch_type": "components", "group_name": "navbar"},           # react
            {"batch_type": "build", "group_name": "maven"},                 # java/build
            {"batch_type": "kubernetes", "group_name": "manifests"},        # k8s
        ]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws"
        )
        assert keys == [
            "ws#github#myrepo#handler-group-summary#chat",
            "ws#github#myrepo#csharp-project-summary#Web",
            "ws#github#myrepo#csharp-services-summary#Auth",
            "ws#github#myrepo#source-module-summary#api",
            "ws#github#myrepo#components-summary#navbar",
            "ws#github#myrepo#build-summary#maven",
            "ws#github#myrepo#kubernetes-summary#manifests",
            "ws#github#myrepo#code-map#myrepo/code-map",
        ]


class TestExpectedCodeMapVectorKeysShardAware:
    """Regression for the 2026-07-08 shard-orphan-cleanup bug: 
    splitter writes shard-suffixed vector keys for oversized units, but
    the pre-fix _expected_code_map_vector_keys only emitted parent
    group_names. Result: whichever shard summary landed before the
    orphan sweep got deleted, breaking merge fan-in silently every run.
    """

    def test_small_unit_still_single_key_without_all_files(self):
        """No all_files -> single-shard default. Backward compat."""
        units = [{
            "batch_type": "handler-group",
            "group_name": "chat",
            "file_paths": ["lambda/chat/handler.py"],
        }]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws", all_files=None,
        )
        assert keys == [
            "ws#github#myrepo#handler-group-summary#chat",
            "ws#github#myrepo#code-map#myrepo/code-map",
        ]

    def test_small_unit_stays_single_key_with_all_files(self):
        """A unit whose files fit under TARGET_SHARD_TOKENS gets one key."""
        units = [{
            "batch_type": "handler-group",
            "group_name": "chat",
            "file_paths": ["lambda/chat/handler.py"],
        }]
        all_files = [{"path": "lambda/chat/handler.py", "type": "blob", "size": 5000}]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws", all_files=all_files,
        )
        assert keys == [
            "ws#github#myrepo#handler-group-summary#chat",
            "ws#github#myrepo#code-map#myrepo/code-map",
        ]

    def test_oversized_unit_emits_shard_suffixed_keys(self):
        """15 files x 40k bytes overflows TARGET_SHARD_TOKENS. Expected
        keys must include ALL shard-suffixed group_names so the orphan
        sweep doesn't delete them when they land."""
        file_paths = [f"lambda/tests/unit/test_x{i}.py" for i in range(15)]
        units = [{
            "batch_type": "handler-group",
            "group_name": "tests",
            "file_paths": file_paths,
        }]
        all_files = [
            {"path": p, "type": "blob", "size": 40_000} for p in file_paths
        ]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws", all_files=all_files,
        )
        # Must have shard-suffixed keys for every shard the splitter emits
        shard_keys = [
            k for k in keys
            if k.startswith("ws#github#myrepo#handler-group-summary#tests#shard-")
        ]
        assert len(shard_keys) >= 2
        # AND the parent key: the merge fan-in stores the unified summary
        # there. (The pre-2026-07-19 version asserted the parent was
        # ABSENT, which made the orphan sweep delete every merged summary
        # on the following sync.)
        assert "ws#github#myrepo#handler-group-summary#tests" in keys
        # shards + merged parent + architectural code-map key
        assert len(keys) == len(shard_keys) + 2
        assert "ws#github#myrepo#code-map#myrepo/code-map" in keys

    def test_sharded_unit_includes_merged_parent_key(self):
        """Regression for the 2026-07-19 merged-parent-orphan bug: the
        merge fan-in writes the unified summary at the PARENT group_name,
        but expected_keys only listed shard-suffixed names. The next
        sync's orphan sweep then deleted the merged summary -- silently,
        since the merge had succeeded and no failed-files row existed --
        and incremental syncs never regenerated it while the unit was
        unchanged (live-hit: app.retrieveit.ai infrastructure-summary,
        deleted 2026-07-19 by the sweep after the 07-18 merge)."""
        file_paths = [f"terraform/f{i}.tf" for i in range(15)]
        units = [{
            "batch_type": "infrastructure",
            "group_name": "infrastructure",
            "file_paths": file_paths,
        }]
        all_files = [
            {"path": p, "type": "blob", "size": 40_000} for p in file_paths
        ]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws", all_files=all_files,
        )
        assert "ws#github#myrepo#infrastructure-summary#infrastructure" in keys
        # Small units are unaffected: exactly one key, no duplicate parent
        small = repo_summary_handler._expected_code_map_vector_keys(
            [{
                "batch_type": "handler-group",
                "group_name": "chat",
                "file_paths": ["lambda/chat/handler.py"],
            }],
            "myrepo", "ws",
            all_files=[{"path": "lambda/chat/handler.py", "type": "blob", "size": 500}],
        )
        assert small == [
            "ws#github#myrepo#handler-group-summary#chat",
            "ws#github#myrepo#code-map#myrepo/code-map",
        ]

    def test_shard_group_names_match_splitter_emissions(self):
        """The keys emitted here must match what the splitter actually
        writes -- otherwise the orphan sweep deletes real shard summaries."""
        file_paths = [f"tests/t{i}.py" for i in range(20)]
        units = [{
            "batch_type": "handler-group",
            "group_name": "tests",
            "file_paths": file_paths,
            "storage_key": "summary#tests",
        }]
        all_files = [
            {"path": p, "type": "blob", "size": 40_000} for p in file_paths
        ]
        expected = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws", all_files=all_files,
        )
        # Now run the actual splitter helper and check the SHARD messages
        # write to keys IN the expected set (parent-storage-key-based
        # merge messages are a separate concern).
        emitted_names = repo_summary_handler._shard_group_names_for_unit(
            units[0], repo_summary_handler._build_file_sizes_map(all_files),
        )
        for name in emitted_names:
            key = f"ws#github#myrepo#handler-group-summary#{name}"
            assert key in expected, (
                f"Shard key {key} not in expected_keys — orphan sweep "
                "will delete it. Splitter + expected-keys are out of sync."
            )


class TestQueueOrphanCodeMapCleanup:
    """The producer-side: process-repo-summary queues the cleanup job after
    fan-out so any handler/group dropped from the tree gets cleaned up."""

    def test_sends_correct_payload(self, monkeypatch):
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1", "k2"], "ws_abc"
        )

        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["action"] == "delete_orphan_code_maps"
        assert body["workspace_id"] == "ws_abc"
        assert body["repo"] == "myrepo"
        assert body["expected_keys"] == ["k1", "k2"]
        # Sent to the delete queue, NOT the code-maps queue
        assert mock_sqs.send_message.call_args[1]["QueueUrl"] == "https://sqs/delete"

    def test_skips_when_no_workspace(self, monkeypatch):
        """Non-workspace repos: the delete-worker action only handles
        workspace-scoped key shapes; skip the round-trip entirely."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], None
        )
        mock_sqs.send_message.assert_not_called()

    def test_skips_when_no_delete_queue_configured(self, monkeypatch):
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(repo_summary_handler, "DELETE_QUEUE", None)

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], "ws_abc"
        )
        mock_sqs.send_message.assert_not_called()

    def test_refuses_empty_expected_keys(self, monkeypatch):
        """Belt+suspenders: delete-worker also defends, but skip the
        round-trip when there's nothing to compare against."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", [], "ws_abc"
        )
        mock_sqs.send_message.assert_not_called()

    def test_sqs_send_failure_is_swallowed(self, monkeypatch):
        """Cleanup is best-effort. If the SQS send fails, log and continue -
        don't break the regen path."""
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "Throttle", "Message": "x"}}, "SendMessage"
        )
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        # Must not raise
        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], "ws_abc"
        )


class TestSendCodeUnitsToSqs:
    """send_code_units_to_sqs ONLY does the fan-out. Cleanup queueing is
    the caller's responsibility (separated to support the
    full-set-vs-incremental-set distinction)."""

    def test_fanout_no_cleanup_call(self, monkeypatch):
        """send_code_units_to_sqs must NOT enqueue cleanup. The handler
        does that with the full pre-incremental code_units."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "CODE_MAPS_QUEUE_URL", "https://sqs/codemaps"
        )
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        units = [
            {
                "batch_type": "handler-group",
                "group_name": "chat",
                "file_paths": ["lambda/chat/handler.py"],
                "storage_key": "k1",
            },
            {
                "batch_type": "handler-group",
                "group_name": "github-sync",
                "file_paths": ["lambda/github-sync/handler.py"],
                "storage_key": "k2",
            },
        ]

        repo_summary_handler.send_code_units_to_sqs(
            units, "myrepo", "owner/myrepo", workspace_id="ws_abc"
        )

        # 2 unit messages, NO cleanup message
        assert mock_sqs.send_message.call_count == 2
        for call in mock_sqs.send_message.call_args_list:
            assert call[1]["QueueUrl"] == "https://sqs/codemaps"
            body = json.loads(call[1]["MessageBody"])
            assert body.get("action") != "delete_orphan_code_maps"


class TestFullRegenMessageStamp:
    """ADR-055: process-repo-summary stamps ``full_regen`` on every dispatched
    SQS message so the last-close batch in process-batch-summary knows it may
    run the whole-repo resolution post-pass. Stamped only on a full regen
    (incremental filter did not shrink the code-unit set)."""

    _UNIT = {
        "file_paths": ["lambda/a/handler.py", "lambda/a/util.py"],
        "storage_key": "code-map#a",
        "group_name": "a",
        "batch_type": "lambda",
    }

    def _build(self, *, full_regen, file_sizes=None):
        return repo_summary_handler._build_shard_messages_for_unit(
            code_unit=self._UNIT,
            repo="repo",
            repo_project="owner/repo",
            workspace_id="ws1",
            commit_sha="sha1",
            commit_timestamp=123,
            provider="github",
            slug=None,
            file_sizes=file_sizes or {},
            full_regen=full_regen,
        )

    def test_full_regen_true_stamps_single_shard(self):
        msgs = self._build(full_regen=True)
        assert len(msgs) == 1
        assert msgs[0]["full_regen"] is True

    def test_full_regen_false_omits_flag(self):
        msgs = self._build(full_regen=False)
        assert len(msgs) == 1
        assert "full_regen" not in msgs[0]

    def test_default_omits_flag(self):
        # Keyword defaults to False -- incremental message shape unchanged.
        msgs = repo_summary_handler._build_shard_messages_for_unit(
            code_unit=self._UNIT,
            repo="repo",
            repo_project="owner/repo",
            workspace_id="ws1",
            commit_sha="sha1",
            commit_timestamp=123,
            provider="github",
            slug=None,
            file_sizes={},
        )
        assert all("full_regen" not in m for m in msgs)

    def test_full_regen_stamps_shards_and_merge_when_sharded(self):
        # Oversized unit -> N shard messages + 1 merge; every one carries the
        # flag so whichever batch closes last sees it.
        big = {p: 5_000_000 for p in self._UNIT["file_paths"]}
        msgs = self._build(full_regen=True, file_sizes=big)
        assert len(msgs) > 1
        assert any(m["message_type"] == "merge" for m in msgs)
        assert all(m["full_regen"] is True for m in msgs)


class TestConfigContractChangeForcesFullRegen:
    """A change to .outcomeops.yaml (the discovery/extraction contract) must
    force a full regen. Incremental re-ingestion keeps only code units whose
    SOURCE changed, so a config-only change (which touches no code unit's
    source) would otherwise silently not take effect until an unrelated source
    change. Language-agnostic: the check sits above the per-language backends."""

    def test_config_filename_constant(self):
        assert repo_summary_handler.REPO_CONFIG_FILENAME == ".outcomeops.yaml"

    def test_config_change_detected(self):
        changed = {".outcomeops.yaml", "README.md"}
        assert repo_summary_handler._config_contract_changed(changed) is True

    def test_source_only_change_not_detected(self):
        changed = {"src/mathx.ts", "src/strutil.ts"}
        assert repo_summary_handler._config_contract_changed(changed) is False

    def test_empty_change_set_not_detected(self):
        assert repo_summary_handler._config_contract_changed(set()) is False

    def test_accepts_any_iterable_of_changed_files(self):
        # change_result.changed_files is a list upstream; the helper must handle
        # a plain list, not only a set.
        assert repo_summary_handler._config_contract_changed(
            ["a.py", ".outcomeops.yaml"]
        ) is True
        assert repo_summary_handler._config_contract_changed(["a.py"]) is False


class TestArchSummaryWorkspaceAttribution:
    """Code-map arch-summary Bedrock spend must attribute to the owning
    workspace in the audit row, not WS#SYSTEM (where the analytics
    aggregator skips it). Regression for pipeline spend attribution."""

    def test_generate_architectural_summary_threads_workspace_id(self):
        from types import SimpleNamespace

        resp = SimpleNamespace(
            text="arch summary",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=40),
        )
        with patch.object(
            repo_summary_handler, "invoke_model", return_value=resp
        ), patch.object(
            repo_summary_handler, "log_audit_entry"
        ) as mock_audit:
            result = repo_summary_handler.generate_architectural_summary(
                "my-repo", "application", ["a.py", "b.py"],
                workspace_id="ws_eng",
            )

        assert result == "arch summary"
        mock_audit.assert_called_once()
        assert mock_audit.call_args.kwargs["workspace_id"] == "ws_eng"

    def test_generate_architectural_summary_defaults_empty_workspace(self):
        """Legacy global code-maps (no workspace) still log, with ''."""
        from types import SimpleNamespace

        resp = SimpleNamespace(
            text="arch summary",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        with patch.object(
            repo_summary_handler, "invoke_model", return_value=resp
        ), patch.object(
            repo_summary_handler, "log_audit_entry"
        ) as mock_audit:
            repo_summary_handler.generate_architectural_summary(
                "legacy-repo", "application", ["a.py"],
            )

        assert mock_audit.call_args.kwargs["workspace_id"] == ""


class TestZeroChangedUnitsAdvancesCodeMapSha:
    """A GitHub commit whose incremental filter matches zero code units
    (docs/lockfile-only) has no work to wait on, so last_code_map_sha and
    last_gkg_sha must advance directly instead of staying on the previous
    SHA (which looked identical to a failed regen)."""

    _UNIT = {
        "name": "api", "group_name": "api", "batch_type": "handler-group",
        "storage_key": "api", "file_paths": ["src/api.py"],
    }

    def _event(self, **overrides):
        body = {
            "repo": "my-repo",
            "repo_project": "acme/my-repo",
            "repo_type": "application",
            "all_files": ["src/api.py", "README.md"],
            "workspace_id": "ws_eng",
            "installation_id": "123",
            "discover_code_units": True,
            "last_commit_sha": "a" * 40,
            "current_commit_sha": "b" * 40,
            "commit_timestamp": 1700000000,
        }
        body.update(overrides)
        return {"Records": [{"messageId": "m1", "receiptHandle": "h1",
                             "body": json.dumps(body)}]}

    def _run(self, changed_files, **overrides):
        backend = MagicMock()
        backend.detect_changes.return_value = MagicMock(changed_files=changed_files)
        with patch.object(repo_summary_handler, "load_config"), \
                patch.object(repo_summary_handler, "generate_architectural_summary",
                             return_value="summary"), \
                patch.object(repo_summary_handler, "generate_embedding",
                             return_value=[0.1] * 1024), \
                patch.object(repo_summary_handler, "store_code_map_embedding"), \
                patch.object(repo_summary_handler, "s3_client"), \
                patch.object(repo_summary_handler, "get_installation_token",
                             return_value="tok"), \
                patch.object(repo_summary_handler, "discover_code_units",
                             return_value=[dict(self._UNIT)]), \
                patch.object(repo_summary_handler, "fetch_repo_config", return_value={}), \
                patch.object(repo_summary_handler, "get_backend_for_repo",
                             return_value=backend), \
                patch.object(repo_summary_handler, "_queue_orphan_code_map_cleanup"), \
                patch.object(repo_summary_handler, "_expected_code_map_vector_keys",
                             return_value=[]), \
                patch.object(repo_summary_handler, "_open_code_map_units_ledger"), \
                patch.object(repo_summary_handler, "_close_code_map_dispatch_ledger"), \
                patch.object(repo_summary_handler, "send_code_units_to_sqs") as send, \
                patch.object(repo_summary_handler,
                             "_advance_code_map_without_work") as advance:
            result = repo_summary_handler.handler(self._event(**overrides), None)
        assert result["batchItemFailures"] == []
        return send, advance

    def test_zero_changed_units_advances_without_dispatch(self):
        send, advance = self._run(["README.md"])
        send.assert_not_called()
        advance.assert_called_once_with(
            "ws_eng", "acme/my-repo", "b" * 40, 1700000000)

    def test_changed_unit_dispatches_and_does_not_shortcut(self):
        send, advance = self._run(["src/api.py"])
        send.assert_called_once()
        advance.assert_not_called()

    def test_non_github_provider_never_shortcuts(self):
        # The SHA ledger is GitHub-only; other providers must not write
        # GITHUB_REPO# columns.
        send, advance = self._run(["README.md"], provider="gitlab")
        advance.assert_not_called()


class TestAdvanceCodeMapWithoutWork:
    """The helper itself: close the dispatch stage, then advance both the
    code_map and gkg columns. Best-effort, so a ledger failure never fails
    the sync."""

    def test_closes_dispatch_and_advances_both_pipelines(self):
        from shared import sha_truth  # type: ignore

        ok = sha_truth.AdvanceResult(True, "ok")
        with patch.object(sha_truth, "mark_unit_complete") as mark, \
                patch.object(sha_truth, "advance_sha_without_work",
                             return_value=ok) as advance:
            repo_summary_handler._advance_code_map_without_work(
                "ws_eng", "acme/my-repo", "b" * 40, 1700000000)

        mark.assert_called_once_with(
            "ws_eng", "acme/my-repo", "code_map_dispatch", "b" * 40)
        assert [c.args[2] for c in advance.call_args_list] == ["code_map", "gkg"]
        for call in advance.call_args_list:
            assert call.args[3:] == ("b" * 40, 1700000000)

    def test_ledger_failure_is_swallowed(self):
        from shared import sha_truth  # type: ignore

        with patch.object(sha_truth, "mark_unit_complete",
                          side_effect=RuntimeError("ddb down")), \
                patch.object(sha_truth, "advance_sha_without_work") as advance:
            repo_summary_handler._advance_code_map_without_work(
                "ws_eng", "acme/my-repo", "b" * 40, 1700000000)
        advance.assert_not_called()
