"""Unit tests for delete-worker Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

# Pre-mock boto3 clients before module import
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()
mock_s3vectors_client = MagicMock()


def _load_handler_module():
    """Load or reload the delete-worker handler module."""
    if 'delete_worker_handler' in sys.modules:
        del sys.modules['delete_worker_handler']

    # Add delete-worker dir to path so handler can resolve its sibling imports
    worker_dir = os.path.join(os.path.dirname(__file__), '../../delete-worker')
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)

    handler_path = os.path.join(worker_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("delete_worker_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['delete_worker_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client
    handler_module.s3vectors_client = mock_s3vectors_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
list_s3_keys_with_prefix = handler_module.list_s3_keys_with_prefix
delete_s3_object = handler_module.delete_s3_object
delete_s3_objects_batch = handler_module.delete_s3_objects_batch
delete_vectors_for_s3_key = handler_module.delete_vectors_for_s3_key
delete_vectors_with_prefix = handler_module.delete_vectors_with_prefix
delete_vectors_by_key_prefix = handler_module.delete_vectors_by_key_prefix
extract_page_id_from_s3_key = handler_module.extract_page_id_from_s3_key
find_orphaned_documents = handler_module.find_orphaned_documents
handle_delete_document = handler_module.handle_delete_document
handle_delete_prefix = handler_module.handle_delete_prefix
handle_delete_orphans = handler_module.handle_delete_orphans
_load_vector_config = handler_module._load_vector_config


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_s3_client.reset_mock()
    mock_ssm_client.reset_mock()
    mock_s3vectors_client.reset_mock()
    # Explicitly clear side_effect on method mocks that tests configure with
    # side_effect lists/exceptions -- reset_mock on the parent clears child
    # mocks' call state but consumed iterators can cause StopIteration leaks.
    mock_s3_client.delete_objects.side_effect = None
    mock_s3_client.delete_objects.return_value = {}
    mock_s3_client.delete_object.side_effect = None
    mock_s3_client.delete_object.return_value = {}
    mock_ssm_client.get_parameter.side_effect = None
    mock_ssm_client.get_parameter.return_value = {}
    mock_s3vectors_client.delete_vectors.side_effect = None
    mock_s3vectors_client.delete_vectors.return_value = {}
    mock_s3vectors_client.list_vectors.side_effect = None
    mock_s3vectors_client.list_vectors.return_value = {"vectors": []}
    handler_module._vector_config = None
    handler_module.KB_BUCKET = "test-kb-bucket"
    yield


@pytest.fixture
def vector_config():
    """Set up vector config so _load_vector_config returns cached values."""
    handler_module._vector_config = {"bucket": "vector-bucket", "index": "vector-index"}
    return handler_module._vector_config


@pytest.fixture
def sample_sqs_event():
    """Create a sample SQS event with one record."""
    def _make_event(messages):
        return {
            "Records": [
                {"body": json.dumps(msg)} for msg in messages
            ]
        }
    return _make_event


# ============================================================================
# _load_vector_config
# ============================================================================

class TestLoadVectorConfig:
    """Test loading vector store configuration from SSM."""

    def test_load_vector_config_success(self):
        """Test successful loading of vector config from SSM."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "my-vector-bucket"}},
            {"Parameter": {"Value": "my-vector-index"}},
        ]

        result = _load_vector_config()

        assert result == {"bucket": "my-vector-bucket", "index": "my-vector-index"}
        assert mock_ssm_client.get_parameter.call_count == 2

    def test_load_vector_config_caches_result(self):
        """Test that config is cached after first load."""
        handler_module._vector_config = {"bucket": "cached-bucket", "index": "cached-index"}

        result = _load_vector_config()

        assert result == {"bucket": "cached-bucket", "index": "cached-index"}
        mock_ssm_client.get_parameter.assert_not_called()

    def test_load_vector_config_ssm_error(self):
        """Test raises when SSM call fails."""
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

        with pytest.raises(ClientError):
            _load_vector_config()

    def test_load_vector_config_uses_env_prefix(self):
        """Test that the SSM prefix uses ENV and APP_NAME."""
        handler_module._vector_config = None
        original_env = handler_module.ENV
        original_app = handler_module.APP_NAME
        handler_module.ENV = "staging"
        handler_module.APP_NAME = "my-app"

        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "bucket"}},
            {"Parameter": {"Value": "index"}},
        ]

        try:
            _load_vector_config()
            calls = mock_ssm_client.get_parameter.call_args_list
            assert calls[0][1]["Name"] == "/staging/my-app/s3vectors/bucket-name"
            assert calls[1][1]["Name"] == "/staging/my-app/s3vectors/index-name"
        finally:
            handler_module.ENV = original_env
            handler_module.APP_NAME = original_app


# ============================================================================
# list_s3_keys_with_prefix
# ============================================================================

class TestListS3KeysWithPrefix:
    """Test listing S3 keys by prefix."""

    def test_list_keys_single_page(self):
        """Test listing keys that fit in a single page."""
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "prefix/file1.txt"}, {"Key": "prefix/file2.txt"}]}
        ]

        result = list_s3_keys_with_prefix("prefix/")

        assert result == ["prefix/file1.txt", "prefix/file2.txt"]
        mock_s3_client.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="test-kb-bucket", Prefix="prefix/")

    def test_list_keys_multiple_pages(self):
        """Test listing keys across multiple pages."""
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "prefix/file1.txt"}]},
            {"Contents": [{"Key": "prefix/file2.txt"}]},
        ]

        result = list_s3_keys_with_prefix("prefix/")

        assert result == ["prefix/file1.txt", "prefix/file2.txt"]

    def test_list_keys_empty_result(self):
        """Test listing keys when no objects match."""
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]

        result = list_s3_keys_with_prefix("nonexistent/")

        assert result == []

    def test_list_keys_page_without_contents(self):
        """Test pages that have no Contents key."""
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "prefix/file1.txt"}]},
            {},
            {"Contents": [{"Key": "prefix/file3.txt"}]},
        ]

        result = list_s3_keys_with_prefix("prefix/")

        assert result == ["prefix/file1.txt", "prefix/file3.txt"]

    def test_list_keys_client_error(self):
        """Test returns empty list on S3 error."""
        paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = paginator
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
            "ListObjectsV2",
        )

        result = list_s3_keys_with_prefix("prefix/")

        assert result == []


# ============================================================================
# delete_s3_object
# ============================================================================

class TestDeleteS3Object:
    """Test single S3 object deletion."""

    def test_delete_object_success(self):
        """Test successful deletion returns True."""
        mock_s3_client.delete_object.return_value = {}

        result = delete_s3_object("workspaces/ws_123/confluence/space/page.html")

        assert result is True
        mock_s3_client.delete_object.assert_called_once_with(
            Bucket="test-kb-bucket",
            Key="workspaces/ws_123/confluence/space/page.html",
        )

    def test_delete_object_client_error(self):
        """Test returns False on S3 error."""
        mock_s3_client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "DeleteObject",
        )

        result = delete_s3_object("workspaces/ws_123/bad-key.txt")

        assert result is False


# ============================================================================
# delete_s3_objects_batch
# ============================================================================

class TestDeleteS3ObjectsBatch:
    """Test batch S3 object deletion."""

    def test_batch_delete_empty_list(self):
        """Test returns 0 for empty key list."""
        result = delete_s3_objects_batch([])

        assert result == 0
        mock_s3_client.delete_objects.assert_not_called()

    def test_batch_delete_single_batch(self):
        """Test deleting fewer than 1000 keys in one batch."""
        keys = ["key1", "key2", "key3"]
        mock_s3_client.delete_objects.return_value = {
            "Deleted": [{"Key": k} for k in keys]
        }

        result = delete_s3_objects_batch(keys)

        assert result == 3
        mock_s3_client.delete_objects.assert_called_once_with(
            Bucket="test-kb-bucket",
            Delete={"Objects": [{"Key": "key1"}, {"Key": "key2"}, {"Key": "key3"}]},
        )

    def test_batch_delete_multiple_batches(self):
        """Test keys are split into 1000-key batches."""
        keys = [f"key_{i}" for i in range(1500)]
        mock_s3_client.delete_objects.side_effect = [
            {"Deleted": [{"Key": f"key_{i}"} for i in range(1000)]},
            {"Deleted": [{"Key": f"key_{i}"} for i in range(1000, 1500)]},
        ]

        result = delete_s3_objects_batch(keys)

        assert result == 1500
        assert mock_s3_client.delete_objects.call_count == 2

        # Verify first batch has 1000 keys
        first_call_objects = mock_s3_client.delete_objects.call_args_list[0][1]["Delete"]["Objects"]
        assert len(first_call_objects) == 1000

        # Verify second batch has 500 keys
        second_call_objects = mock_s3_client.delete_objects.call_args_list[1][1]["Delete"]["Objects"]
        assert len(second_call_objects) == 500

    def test_batch_delete_partial_failure(self):
        """Test counts only successfully deleted objects."""
        keys = ["key1", "key2", "key3"]
        mock_s3_client.delete_objects.return_value = {
            "Deleted": [{"Key": "key1"}],
            "Errors": [{"Key": "key2", "Code": "InternalError"}],
        }

        result = delete_s3_objects_batch(keys)

        assert result == 1

    def test_batch_delete_client_error(self):
        """Test handles S3 error gracefully."""
        keys = ["key1", "key2"]
        mock_s3_client.delete_objects.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}},
            "DeleteObjects",
        )

        result = delete_s3_objects_batch(keys)

        assert result == 0

    def test_batch_delete_no_deleted_key_in_response(self):
        """Test handles response without Deleted key."""
        keys = ["key1"]
        mock_s3_client.delete_objects.return_value = {}

        result = delete_s3_objects_batch(keys)

        assert result == 0


# ============================================================================
# delete_vectors_for_s3_key
# ============================================================================

class TestDeleteVectorsForS3Key:
    """Test deleting vectors associated with an S3 key."""

    def test_delete_vectors_success_with_folder(self, vector_config):
        """Test successful vector deletion for key with folder path."""
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_for_s3_key(
            "workspaces/ws_123/confluence/space_key/page_title_12345.html"
        )

        assert result == 100
        mock_s3vectors_client.delete_vectors.assert_called_once()
        call_kwargs = mock_s3vectors_client.delete_vectors.call_args[1]
        assert call_kwargs["vectorBucketName"] == "vector-bucket"
        assert call_kwargs["indexName"] == "vector-index"

        # Verify the keys contain the expected prefix pattern
        keys = call_kwargs["keys"]
        assert len(keys) == 100
        assert keys[0] == "ws_123#confluence#space_key#page_title_12345.html_chunk_0"
        assert keys[99] == "ws_123#confluence#space_key#page_title_12345.html_chunk_99"

    def test_delete_vectors_success_without_folder(self, vector_config):
        """Test vector deletion for key without subfolder."""
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_for_s3_key(
            "workspaces/ws_123/confluence/page.html"
        )

        assert result == 100
        keys = mock_s3vectors_client.delete_vectors.call_args[1]["keys"]
        assert keys[0] == "ws_123#confluence#page.html_chunk_0"

    def test_delete_vectors_nested_folder(self, vector_config):
        """Test vector deletion for key with nested folder path."""
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_for_s3_key(
            "workspaces/ws_123/github/repo/src/main/App.java"
        )

        assert result == 100
        keys = mock_s3vectors_client.delete_vectors.call_args[1]["keys"]
        # folder_path = "repo/src/main", safe_folder = "repo#src#main"
        assert keys[0] == "ws_123#github#repo#src#main#App.java_chunk_0"

    def test_delete_vectors_invalid_key_format(self, vector_config):
        """Test returns 0 for invalid S3 key format."""
        result = delete_vectors_for_s3_key("invalid/key")

        assert result == 0
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_vectors_key_not_starting_with_workspaces(self, vector_config):
        """Test returns 0 when key does not start with 'workspaces'."""
        result = delete_vectors_for_s3_key("other/ws_123/confluence/page.html")

        assert result == 0
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_vectors_too_few_parts(self, vector_config):
        """Test returns 0 when key has fewer than 4 parts."""
        result = delete_vectors_for_s3_key("workspaces/ws_123/conf")

        assert result == 0
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_vectors_client_error(self, vector_config):
        """Test returns 0 on s3vectors error."""
        mock_s3vectors_client.delete_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}},
            "DeleteVectors",
        )

        result = delete_vectors_for_s3_key(
            "workspaces/ws_123/confluence/space/page.html"
        )

        assert result == 0

    def test_delete_vectors_loads_config_from_ssm(self):
        """Test loads vector config from SSM when not cached."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "ssm-bucket"}},
            {"Parameter": {"Value": "ssm-index"}},
        ]
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_for_s3_key(
            "workspaces/ws_123/confluence/space/page.html"
        )

        assert result == 100
        assert mock_ssm_client.get_parameter.call_count == 2


# ============================================================================
# delete_vectors_with_prefix
# ============================================================================

class TestDeleteVectorsWithPrefix:
    """Test deleting vectors matching a prefix pattern."""

    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_vectors_with_prefix_success(self, mock_list, mock_delete_vec):
        """Test deleting vectors for all keys under a prefix."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/space/page1.html",
            "workspaces/ws_123/confluence/space/page2.html",
        ]
        mock_delete_vec.return_value = 100

        result = delete_vectors_with_prefix("ws_123", "confluence", "space/")

        assert result == 200
        mock_list.assert_called_once_with("workspaces/ws_123/confluence/space/")
        assert mock_delete_vec.call_count == 2

    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_vectors_with_prefix_no_keys(self, mock_list, mock_delete_vec):
        """Test no-op when no S3 keys found."""
        mock_list.return_value = []

        result = delete_vectors_with_prefix("ws_123", "confluence", "empty/")

        assert result == 0
        mock_delete_vec.assert_not_called()


# ============================================================================
# delete_vectors_by_key_prefix
# ============================================================================

class TestDeleteVectorsByKeyPrefix:
    """Test deleting vectors by key prefix via list-then-delete."""

    def test_delete_by_prefix_single_page(self, vector_config):
        """Test deleting vectors found in a single list page."""
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [
                {"key": "ws_123#github#repo#code-map#1"},
                {"key": "ws_123#github#repo#code-map#2"},
                {"key": "ws_123#github#other-repo#code-map#1"},
            ],
        }
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 2
        mock_s3vectors_client.delete_vectors.assert_called_once()
        deleted_keys = mock_s3vectors_client.delete_vectors.call_args[1]["keys"]
        assert "ws_123#github#repo#code-map#1" in deleted_keys
        assert "ws_123#github#repo#code-map#2" in deleted_keys
        assert "ws_123#github#other-repo#code-map#1" not in deleted_keys

    def test_delete_by_prefix_paginated(self, vector_config):
        """Test listing vectors across multiple pages."""
        mock_s3vectors_client.list_vectors.side_effect = [
            {
                "vectors": [{"key": "ws_123#github#repo#file1"}],
                "nextToken": "token1",
            },
            {
                "vectors": [{"key": "ws_123#github#repo#file2"}],
            },
        ]
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 2
        assert mock_s3vectors_client.list_vectors.call_count == 2

        # Verify second call includes nextToken
        second_call = mock_s3vectors_client.list_vectors.call_args_list[1][1]
        assert second_call["nextToken"] == "token1"

    def test_delete_by_prefix_no_matches(self, vector_config):
        """Test returns 0 when no vectors match the prefix."""
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [
                {"key": "ws_999#github#other-repo#file"},
            ],
        }

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 0
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_by_prefix_empty_list(self, vector_config):
        """Test returns 0 when vector list is empty."""
        mock_s3vectors_client.list_vectors.return_value = {"vectors": []}

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 0
        mock_s3vectors_client.delete_vectors.assert_not_called()

    def test_delete_by_prefix_list_error(self, vector_config):
        """Test returns 0 when listing vectors fails."""
        mock_s3vectors_client.list_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}},
            "ListVectors",
        )

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 0

    def test_delete_by_prefix_delete_batch_error(self, vector_config):
        """Test handles deletion error in batch."""
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [{"key": "ws_123#github#repo#file1"}],
        }
        mock_s3vectors_client.delete_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}},
            "DeleteVectors",
        )

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 0

    def test_delete_by_prefix_large_batch_split(self, vector_config):
        """Test that more than 100 matching vectors are deleted in batches."""
        # Return 150 matching vectors
        vectors = [{"key": f"ws_123#github#repo#file_{i}"} for i in range(150)]
        mock_s3vectors_client.list_vectors.return_value = {"vectors": vectors}
        mock_s3vectors_client.delete_vectors.return_value = {}

        result = delete_vectors_by_key_prefix("ws_123#github#repo#")

        assert result == 150
        assert mock_s3vectors_client.delete_vectors.call_count == 2

        # First batch should have 100 keys
        first_batch = mock_s3vectors_client.delete_vectors.call_args_list[0][1]["keys"]
        assert len(first_batch) == 100

        # Second batch should have 50 keys
        second_batch = mock_s3vectors_client.delete_vectors.call_args_list[1][1]["keys"]
        assert len(second_batch) == 50

    def test_delete_by_prefix_uses_vector_config(self, vector_config):
        """Test that list_vectors and delete_vectors use the vector config."""
        mock_s3vectors_client.list_vectors.return_value = {
            "vectors": [{"key": "ws_123#github#repo#f1"}],
        }
        mock_s3vectors_client.delete_vectors.return_value = {}

        delete_vectors_by_key_prefix("ws_123#github#repo#")

        list_kwargs = mock_s3vectors_client.list_vectors.call_args[1]
        assert list_kwargs["vectorBucketName"] == "vector-bucket"
        assert list_kwargs["indexName"] == "vector-index"

        del_kwargs = mock_s3vectors_client.delete_vectors.call_args[1]
        assert del_kwargs["vectorBucketName"] == "vector-bucket"
        assert del_kwargs["indexName"] == "vector-index"


# ============================================================================
# extract_page_id_from_s3_key
# ============================================================================

class TestExtractPageIdFromS3Key:
    """Test extracting Confluence page IDs from S3 keys."""

    def test_extract_standard_key(self):
        """Test extracting page ID from standard Confluence key."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/My_Page_Title_12345.html"
        )
        assert result == "12345"

    def test_extract_multiple_underscores(self):
        """Test extracts last underscore segment as ID."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/A_Long_Page_Name_67890.html"
        )
        assert result == "67890"

    def test_extract_no_html_extension(self):
        """Test extraction works for non-html files."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/file_99999.txt"
        )
        # No .html removal, but underscore splitting still works
        assert result == "99999.txt"

    def test_extract_no_underscore(self):
        """Test returns empty string when no underscore in filename."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/nounderscorefile.html"
        )
        assert result == ""

    def test_extract_html_extension_removed(self):
        """Test .html extension is stripped before extracting ID."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/title_99.html"
        )
        assert result == "99"

    def test_extract_deep_path(self):
        """Test extracts from deeply nested path."""
        result = extract_page_id_from_s3_key(
            "workspaces/ws_123/confluence/SPACE/sub/path/title_42.html"
        )
        assert result == "42"

    def test_extract_only_filename_matters(self):
        """Test that only the last path segment is considered."""
        result = extract_page_id_from_s3_key("a/b/c/d/name_555.html")
        assert result == "555"


# ============================================================================
# find_orphaned_documents
# ============================================================================

class TestFindOrphanedDocuments:
    """Test orphan detection logic."""

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_confluence(self, mock_list):
        """Test finding orphaned Confluence pages."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/SPACE/Page_One_111.html",
            "workspaces/ws_123/confluence/SPACE/Page_Two_222.html",
            "workspaces/ws_123/confluence/SPACE/Page_Three_333.html",
        ]

        current_ids = {"111", "333"}
        result = find_orphaned_documents("ws_123", "confluence", "SPACE", current_ids)

        assert len(result) == 1
        assert "Page_Two_222.html" in result[0]

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_no_orphans(self, mock_list):
        """Test when all documents are current."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/SPACE/Page_111.html",
            "workspaces/ws_123/confluence/SPACE/Page_222.html",
        ]

        current_ids = {"111", "222"}
        result = find_orphaned_documents("ws_123", "confluence", "SPACE", current_ids)

        assert result == []

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_all_orphaned(self, mock_list):
        """Test when all documents are orphaned."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/SPACE/Page_111.html",
            "workspaces/ws_123/confluence/SPACE/Page_222.html",
        ]

        current_ids = {"999"}
        result = find_orphaned_documents("ws_123", "confluence", "SPACE", current_ids)

        assert len(result) == 2

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_empty_s3(self, mock_list):
        """Test when no S3 keys exist."""
        mock_list.return_value = []

        result = find_orphaned_documents("ws_123", "confluence", "SPACE", {"111"})

        assert result == []

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_non_confluence_source(self, mock_list):
        """Test orphan detection for non-confluence source uses different ID extraction."""
        mock_list.return_value = [
            "workspaces/ws_123/jira/project/TICKET-1_summary.json",
            "workspaces/ws_123/jira/project/TICKET-2_details.json",
        ]

        # Non-confluence: extracts ID as first segment before underscore
        current_ids = {"TICKET-1"}
        result = find_orphaned_documents("ws_123", "jira", "project", current_ids)

        assert len(result) == 1
        assert "TICKET-2" in result[0]

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_constructs_correct_prefix(self, mock_list):
        """Test that the S3 prefix is constructed correctly."""
        mock_list.return_value = []

        find_orphaned_documents("ws_abc", "confluence", "MY_SPACE", {"1"})

        mock_list.assert_called_once_with("workspaces/ws_abc/confluence/MY_SPACE")

    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_find_orphans_no_underscore_in_filename(self, mock_list):
        """Test files without underscore are skipped (empty ID)."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/SPACE/nounderscorefile.html",
        ]

        result = find_orphaned_documents("ws_123", "confluence", "SPACE", {"111"})

        # Empty string ID from extract means it won't match current_ids
        # but also won't be added because empty string is falsy
        assert result == []


# ============================================================================
# handle_delete_document
# ============================================================================

class TestHandleDeleteDocument:
    """Test single document deletion handler."""

    @patch.object(handler_module, 'delete_s3_object')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    def test_delete_document_success(self, mock_del_vectors, mock_del_s3):
        """Test successful document deletion."""
        mock_del_vectors.return_value = 100
        mock_del_s3.return_value = True

        result = handle_delete_document({
            "action": "delete_document",
            "s3_key": "workspaces/ws_123/confluence/space/page.html",
        })

        assert result["status"] == "success"
        assert result["action"] == "delete_document"
        assert result["s3_key"] == "workspaces/ws_123/confluence/space/page.html"
        assert result["vectors_deleted"] == 100
        assert result["s3_deleted"] is True

    @patch.object(handler_module, 'delete_s3_object')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    def test_delete_document_vectors_first(self, mock_del_vectors, mock_del_s3):
        """Test that vectors are deleted before S3 object."""
        call_order = []
        mock_del_vectors.side_effect = lambda key: call_order.append("vectors") or 100
        mock_del_s3.side_effect = lambda key: call_order.append("s3") or True

        handle_delete_document({
            "s3_key": "workspaces/ws_123/confluence/space/page.html",
        })

        assert call_order == ["vectors", "s3"]

    def test_delete_document_missing_s3_key(self):
        """Test error when s3_key is missing."""
        result = handle_delete_document({"action": "delete_document"})

        assert result["status"] == "error"
        assert "Missing s3_key" in result["error"]

    def test_delete_document_empty_s3_key(self):
        """Test error when s3_key is empty string."""
        result = handle_delete_document({"action": "delete_document", "s3_key": ""})

        assert result["status"] == "error"
        assert "Missing s3_key" in result["error"]

    @patch.object(handler_module, 'delete_s3_object')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    def test_delete_document_vector_failure_still_deletes_s3(self, mock_del_vectors, mock_del_s3):
        """Test S3 deletion proceeds even if vector deletion fails."""
        mock_del_vectors.return_value = 0
        mock_del_s3.return_value = True

        result = handle_delete_document({
            "s3_key": "workspaces/ws_123/confluence/space/page.html",
        })

        assert result["status"] == "success"
        assert result["vectors_deleted"] == 0
        assert result["s3_deleted"] is True
        mock_del_s3.assert_called_once()


# ============================================================================
# handle_delete_prefix
# ============================================================================

class TestHandleDeletePrefix:
    """Test prefix-based bulk deletion handler."""

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_by_key_prefix')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_prefix_success(self, mock_list, mock_del_vec, mock_del_vec_prefix, mock_del_s3):
        """Test successful prefix deletion."""
        mock_list.return_value = [
            "workspaces/ws_123/confluence/space/p1.html",
            "workspaces/ws_123/confluence/space/p2.html",
        ]
        mock_del_vec.return_value = 100
        mock_del_vec_prefix.return_value = 50
        mock_del_s3.return_value = 2

        result = handle_delete_prefix({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "space/",
        })

        assert result["status"] == "success"
        assert result["action"] == "delete_prefix"
        assert result["prefix"] == "workspaces/ws_123/confluence/space/"
        assert result["documents_deleted"] == 2
        assert result["vectors_deleted"] == 250  # 2 * 100 + 50

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_prefix_empty_prefix(self, mock_list, mock_del_vec, mock_del_s3):
        """Test deletion with empty prefix (deletes all for source)."""
        mock_list.return_value = ["workspaces/ws_123/confluence/page.html"]
        mock_del_vec.return_value = 100
        mock_del_s3.return_value = 1

        result = handle_delete_prefix({
            "workspace_id": "ws_123",
            "source": "confluence",
        })

        assert result["status"] == "success"
        mock_list.assert_called_once_with("workspaces/ws_123/confluence/")

    def test_delete_prefix_missing_workspace_id(self):
        """Test error when workspace_id is missing."""
        result = handle_delete_prefix({
            "source": "confluence",
            "prefix": "space/",
        })

        assert result["status"] == "error"
        assert "Missing workspace_id or source" in result["error"]

    def test_delete_prefix_missing_source(self):
        """Test error when source is missing."""
        result = handle_delete_prefix({
            "workspace_id": "ws_123",
            "prefix": "space/",
        })

        assert result["status"] == "error"
        assert "Missing workspace_id or source" in result["error"]

    def test_delete_prefix_missing_both(self):
        """Test error when both workspace_id and source are missing."""
        result = handle_delete_prefix({"prefix": "space/"})

        assert result["status"] == "error"

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_by_key_prefix')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_prefix_no_documents(self, mock_list, mock_del_vec, mock_del_vec_prefix, mock_del_s3):
        """Test prefix deletion when no documents exist."""
        mock_list.return_value = []
        mock_del_vec_prefix.return_value = 0
        mock_del_s3.return_value = 0

        result = handle_delete_prefix({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "empty_space/",
        })

        assert result["status"] == "success"
        assert result["documents_deleted"] == 0
        mock_del_vec.assert_not_called()

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_by_key_prefix')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_prefix_github_includes_vector_key_prefix(
        self, mock_list, mock_del_vec, mock_del_vec_prefix, mock_del_s3
    ):
        """Test that GitHub prefix deletion also uses delete_vectors_by_key_prefix."""
        mock_list.return_value = [
            "workspaces/ws_123/github/my-repo/code-map.json",
        ]
        mock_del_vec.return_value = 100
        mock_del_vec_prefix.return_value = 25
        mock_del_s3.return_value = 1

        result = handle_delete_prefix({
            "workspace_id": "ws_123",
            "source": "github",
            "prefix": "my-repo",
        })

        assert result["status"] == "success"
        mock_del_vec_prefix.assert_called_once_with("ws_123#github#my-repo#")
        assert result["vectors_deleted"] == 125  # 100 + 25

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_by_key_prefix')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'list_s3_keys_with_prefix')
    def test_delete_prefix_no_prefix_skips_vector_key_prefix(
        self, mock_list, mock_del_vec, mock_del_vec_prefix, mock_del_s3
    ):
        """Test that empty prefix skips delete_vectors_by_key_prefix."""
        mock_list.return_value = ["workspaces/ws_123/confluence/page.html"]
        mock_del_vec.return_value = 100
        mock_del_s3.return_value = 1

        handle_delete_prefix({
            "workspace_id": "ws_123",
            "source": "confluence",
            # No prefix or empty prefix
        })

        mock_del_vec_prefix.assert_not_called()


# ============================================================================
# handle_delete_orphans
# ============================================================================

class TestHandleDeleteOrphans:
    """Test orphan deletion handler."""

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'find_orphaned_documents')
    def test_delete_orphans_success(self, mock_find, mock_del_vec, mock_del_s3):
        """Test successful orphan deletion."""
        mock_find.return_value = [
            "workspaces/ws_123/confluence/SPACE/Old_Page_999.html",
        ]
        mock_del_vec.return_value = 100
        mock_del_s3.return_value = 1

        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
            "current_ids": ["111", "222"],
        })

        assert result["status"] == "success"
        assert result["action"] == "delete_orphans"
        assert result["orphans_found"] == 1
        assert result["orphans_deleted"] == 1
        assert result["vectors_deleted"] == 100

    @patch.object(handler_module, 'find_orphaned_documents')
    def test_delete_orphans_no_orphans(self, mock_find):
        """Test when no orphans are found."""
        mock_find.return_value = []

        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
            "current_ids": ["111"],
        })

        assert result["status"] == "success"
        assert result["orphans_found"] == 0
        assert result["orphans_deleted"] == 0

    def test_delete_orphans_missing_workspace_id(self):
        """Test error when workspace_id is missing."""
        result = handle_delete_orphans({
            "source": "confluence",
            "current_ids": ["111"],
        })

        assert result["status"] == "error"
        assert "Missing workspace_id or source" in result["error"]

    def test_delete_orphans_missing_source(self):
        """Test error when source is missing."""
        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "current_ids": ["111"],
        })

        assert result["status"] == "error"
        assert "Missing workspace_id or source" in result["error"]

    def test_delete_orphans_no_current_ids(self):
        """Test skipped when no current_ids provided."""
        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
        })

        assert result["status"] == "skipped"
        assert "No current_ids" in result["reason"]

    def test_delete_orphans_empty_current_ids(self):
        """Test skipped when current_ids is empty list."""
        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
            "current_ids": [],
        })

        assert result["status"] == "skipped"

    @patch.object(handler_module, 'delete_s3_objects_batch')
    @patch.object(handler_module, 'delete_vectors_for_s3_key')
    @patch.object(handler_module, 'find_orphaned_documents')
    def test_delete_orphans_multiple_orphans(self, mock_find, mock_del_vec, mock_del_s3):
        """Test deleting multiple orphaned documents."""
        mock_find.return_value = [
            "workspaces/ws_123/confluence/SPACE/Page_A_888.html",
            "workspaces/ws_123/confluence/SPACE/Page_B_999.html",
        ]
        mock_del_vec.return_value = 100
        mock_del_s3.return_value = 2

        result = handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
            "current_ids": ["111"],
        })

        assert result["orphans_found"] == 2
        assert result["orphans_deleted"] == 2
        assert result["vectors_deleted"] == 200
        assert mock_del_vec.call_count == 2

    @patch.object(handler_module, 'find_orphaned_documents')
    def test_delete_orphans_default_empty_prefix(self, mock_find):
        """Test that prefix defaults to empty string."""
        mock_find.return_value = []

        handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "current_ids": ["111"],
        })

        mock_find.assert_called_once_with("ws_123", "confluence", "", {"111"})

    @patch.object(handler_module, 'find_orphaned_documents')
    def test_delete_orphans_converts_ids_to_set(self, mock_find):
        """Test that current_ids list is converted to a set."""
        mock_find.return_value = []

        handle_delete_orphans({
            "workspace_id": "ws_123",
            "source": "confluence",
            "prefix": "SPACE",
            "current_ids": ["111", "222", "111"],  # Duplicate
        })

        call_args = mock_find.call_args[0]
        assert isinstance(call_args[3], set)
        assert call_args[3] == {"111", "222"}


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler (SQS-triggered)."""

    @patch.object(handler_module, 'handle_delete_document')
    def test_processes_delete_document(self, mock_handle):
        """Test handler routes delete_document action."""
        mock_handle.return_value = {"status": "success", "action": "delete_document"}

        event = {
            "Records": [
                {"body": json.dumps({
                    "action": "delete_document",
                    "s3_key": "workspaces/ws_123/confluence/page.html",
                })}
            ]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "success"
        mock_handle.assert_called_once()

    @patch.object(handler_module, 'handle_delete_prefix')
    def test_processes_delete_prefix(self, mock_handle):
        """Test handler routes delete_prefix action."""
        mock_handle.return_value = {"status": "success", "action": "delete_prefix"}

        event = {
            "Records": [
                {"body": json.dumps({
                    "action": "delete_prefix",
                    "workspace_id": "ws_123",
                    "source": "confluence",
                    "prefix": "space/",
                })}
            ]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        mock_handle.assert_called_once()

    @patch.object(handler_module, 'handle_delete_orphans')
    def test_processes_delete_orphans(self, mock_handle):
        """Test handler routes delete_orphans action."""
        mock_handle.return_value = {"status": "success", "action": "delete_orphans"}

        event = {
            "Records": [
                {"body": json.dumps({
                    "action": "delete_orphans",
                    "workspace_id": "ws_123",
                    "source": "confluence",
                    "current_ids": ["111"],
                })}
            ]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"

    def test_handles_unknown_action(self):
        """Test handler skips unknown actions."""
        event = {
            "Records": [
                {"body": json.dumps({"action": "unknown_action"})}
            ]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "skipped"
        assert "Unknown action" in body["results"][0]["reason"]

    def test_handles_missing_action(self):
        """Test handler skips message with no action field."""
        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws_123"})}
            ]
        }

        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "skipped"
        assert "Unknown action: None" in body["results"][0]["reason"]

    def test_handles_invalid_json(self):
        """Test handler handles invalid JSON gracefully."""
        event = {"Records": [{"body": "not valid json"}]}

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"
        assert "Invalid JSON" in body["results"][0]["error"]

    def test_handles_missing_records(self):
        """Test handler handles missing Records key."""
        result = lambda_handler({}, None)

        assert result["statusCode"] == 400
        assert result["body"] == "Invalid event"

    @patch.object(handler_module, 'handle_delete_document')
    @patch.object(handler_module, 'handle_delete_prefix')
    def test_handles_multiple_records(self, mock_prefix, mock_doc):
        """Test handler processes multiple SQS records."""
        mock_doc.return_value = {"status": "success", "action": "delete_document"}
        mock_prefix.return_value = {"status": "success", "action": "delete_prefix"}

        event = {
            "Records": [
                {"body": json.dumps({
                    "action": "delete_document",
                    "s3_key": "workspaces/ws_123/confluence/page.html",
                })},
                {"body": json.dumps({
                    "action": "delete_prefix",
                    "workspace_id": "ws_123",
                    "source": "confluence",
                    "prefix": "space/",
                })},
            ]
        }

        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert len(body["results"]) == 2
        assert body["results"][0]["status"] == "success"
        assert body["results"][1]["status"] == "success"
        mock_doc.assert_called_once()
        mock_prefix.assert_called_once()

    @patch.object(handler_module, 'handle_delete_document')
    def test_handles_processing_exception(self, mock_handle):
        """Test handler catches unexpected exceptions."""
        mock_handle.side_effect = Exception("Something broke")

        event = {
            "Records": [
                {"body": json.dumps({
                    "action": "delete_document",
                    "s3_key": "workspaces/ws_123/page.html",
                })}
            ]
        }

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"
        assert "Something broke" in body["results"][0]["error"]

    @patch.object(handler_module, 'handle_delete_document')
    def test_mixed_success_and_failure(self, mock_handle):
        """Test handler counts successes correctly with mixed results."""
        mock_handle.side_effect = [
            {"status": "success", "action": "delete_document"},
            {"status": "error", "error": "Something failed"},
        ]

        event = {
            "Records": [
                {"body": json.dumps({"action": "delete_document", "s3_key": "key1"})},
                {"body": json.dumps({"action": "delete_document", "s3_key": "key2"})},
            ]
        }

        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][1]["status"] == "error"
        assert body["message"] == "Delete processing completed"

    def test_empty_records_list(self):
        """Test handler processes empty Records list."""
        event = {"Records": []}

        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"] == []

    @patch.object(handler_module, 'handle_delete_document')
    def test_result_body_is_valid_json(self, mock_handle):
        """Test response body is valid JSON string."""
        mock_handle.return_value = {"status": "success"}

        event = {
            "Records": [
                {"body": json.dumps({"action": "delete_document", "s3_key": "k"})}
            ]
        }

        result = lambda_handler(event, None)

        body = json.loads(result["body"])
        assert "message" in body
        assert "results" in body
        assert isinstance(body["results"], list)
