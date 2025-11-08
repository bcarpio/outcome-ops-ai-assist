"""Unit tests for state tracker."""

import pytest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime
import importlib.util
from botocore.exceptions import ClientError

# Load the state tracker module
state_tracker_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/state_tracker.py')
spec = importlib.util.spec_from_file_location("state_tracker", state_tracker_path)
state_tracker_module = importlib.util.module_from_spec(spec)
sys.modules['state_tracker'] = state_tracker_module
spec.loader.exec_module(state_tracker_module)

# Import class
StateTracker = state_tracker_module.StateTracker


class TestStateTracker:
    """Test StateTracker class."""

    def setup_method(self):
        """Set up test state tracker."""
        self.mock_dynamodb = MagicMock()
        self.table_name = "test-code-maps-table"
        self.tracker = StateTracker(self.mock_dynamodb, self.table_name)

    def test_initialization(self):
        """Test StateTracker initialization."""
        # Assert
        assert self.tracker.dynamodb_client == self.mock_dynamodb
        assert self.tracker.table_name == self.table_name

    def test_get_last_state_success(self):
        """Test getting last state for a repository."""
        # Arrange
        repo = "test-repo"
        self.mock_dynamodb.get_item.return_value = {
            "Item": {
                "PK": {"S": f"repo#{repo}"},
                "SK": {"S": "state#last-processed"},
                "commit_sha": {"S": "abc123def456"},
                "timestamp": {"S": "2025-01-15T10:00:00.000Z"},
                "files_processed": {"N": "150"},
                "batches_sent": {"N": "25"}
            }
        }

        # Act
        state = self.tracker.get_last_state(repo)

        # Assert
        assert state is not None
        assert state["commit_sha"] == "abc123def456"
        assert state["timestamp"] == "2025-01-15T10:00:00.000Z"
        assert state["files_processed"] == 150
        assert state["batches_sent"] == 25

        self.mock_dynamodb.get_item.assert_called_once_with(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"repo#{repo}"},
                "SK": {"S": "state#last-processed"}
            }
        )

    def test_get_last_state_not_found(self):
        """Test getting last state when no previous state exists."""
        # Arrange
        self.mock_dynamodb.get_item.return_value = {}

        # Act
        state = self.tracker.get_last_state("nonexistent-repo")

        # Assert
        assert state is None

    def test_get_last_state_dynamodb_error(self):
        """Test handling DynamoDB error when getting state."""
        # Arrange
        self.mock_dynamodb.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "GetItem"
        )

        # Act
        state = self.tracker.get_last_state("test-repo")

        # Assert
        assert state is None

    def test_get_last_state_table_not_found(self):
        """Test handling ResourceNotFoundException."""
        # Arrange
        self.mock_dynamodb.get_item.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException"}}, "GetItem"
        )

        # Act
        state = self.tracker.get_last_state("test-repo")

        # Assert
        assert state is None

    def test_save_state_success(self):
        """Test saving state successfully."""
        # Arrange
        repo = "test-repo"
        commit_sha = "abc123def456"
        files_processed = 100
        batches_sent = 20

        self.mock_dynamodb.put_item.return_value = {}

        # Act
        result = self.tracker.save_state(repo, commit_sha, files_processed, batches_sent)

        # Assert
        assert result is True

        # Verify put_item was called with correct parameters
        call_kwargs = self.mock_dynamodb.put_item.call_args[1]
        assert call_kwargs["TableName"] == self.table_name
        assert call_kwargs["Item"]["PK"]["S"] == f"repo#{repo}"
        assert call_kwargs["Item"]["SK"]["S"] == "state#last-processed"
        assert call_kwargs["Item"]["commit_sha"]["S"] == commit_sha
        assert call_kwargs["Item"]["files_processed"]["N"] == str(files_processed)
        assert call_kwargs["Item"]["batches_sent"]["N"] == str(batches_sent)
        assert "timestamp" in call_kwargs["Item"]

    def test_save_state_with_defaults(self):
        """Test saving state with default values."""
        # Arrange
        repo = "test-repo"
        commit_sha = "def456"

        self.mock_dynamodb.put_item.return_value = {}

        # Act
        result = self.tracker.save_state(repo, commit_sha)

        # Assert
        assert result is True

        call_kwargs = self.mock_dynamodb.put_item.call_args[1]
        assert call_kwargs["Item"]["files_processed"]["N"] == "0"
        assert call_kwargs["Item"]["batches_sent"]["N"] == "0"

    def test_save_state_failure(self):
        """Test handling DynamoDB error when saving state."""
        # Arrange
        self.mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "PutItem"
        )

        # Act
        result = self.tracker.save_state("test-repo", "abc123")

        # Assert
        assert result is False

    def test_clear_state_success(self):
        """Test clearing state successfully."""
        # Arrange
        repo = "test-repo"
        self.mock_dynamodb.delete_item.return_value = {}

        # Act
        result = self.tracker.clear_state(repo)

        # Assert
        assert result is True

        self.mock_dynamodb.delete_item.assert_called_once_with(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"repo#{repo}"},
                "SK": {"S": "state#last-processed"}
            }
        )

    def test_clear_state_failure(self):
        """Test handling DynamoDB error when clearing state."""
        # Arrange
        self.mock_dynamodb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "DeleteItem"
        )

        # Act
        result = self.tracker.clear_state("test-repo")

        # Assert
        assert result is False

    def test_list_all_states_success(self):
        """Test listing all repository states."""
        # Arrange
        self.mock_dynamodb.scan.return_value = {
            "Items": [
                {
                    "PK": {"S": "repo#repo1"},
                    "SK": {"S": "state#last-processed"},
                    "commit_sha": {"S": "sha1"},
                    "timestamp": {"S": "2025-01-15T10:00:00Z"},
                    "files_processed": {"N": "100"},
                    "batches_sent": {"N": "10"}
                },
                {
                    "PK": {"S": "repo#repo2"},
                    "SK": {"S": "state#last-processed"},
                    "commit_sha": {"S": "sha2"},
                    "timestamp": {"S": "2025-01-16T10:00:00Z"},
                    "files_processed": {"N": "200"},
                    "batches_sent": {"N": "20"}
                }
            ]
        }

        # Act
        states = self.tracker.list_all_states()

        # Assert
        assert len(states) == 2
        assert "repo1" in states
        assert "repo2" in states
        assert states["repo1"]["commit_sha"] == "sha1"
        assert states["repo1"]["files_processed"] == 100
        assert states["repo2"]["commit_sha"] == "sha2"
        assert states["repo2"]["batches_sent"] == 20

    def test_list_all_states_empty(self):
        """Test listing states when no states exist."""
        # Arrange
        self.mock_dynamodb.scan.return_value = {"Items": []}

        # Act
        states = self.tracker.list_all_states()

        # Assert
        assert states == {}

    def test_list_all_states_filters_correctly(self):
        """Test that list_all_states filters for state items."""
        # Arrange
        self.mock_dynamodb.scan.return_value = {
            "Items": [
                {
                    "PK": {"S": "repo#repo1"},
                    "SK": {"S": "state#last-processed"},
                    "commit_sha": {"S": "sha1"},
                    "timestamp": {"S": "2025-01-15T10:00:00Z"},
                    "files_processed": {"N": "100"},
                    "batches_sent": {"N": "10"}
                },
                {
                    "PK": {"S": "repo#repo1"},
                    "SK": {"S": "summary#handler#auth"},  # Not a state item
                    "content": {"S": "Summary..."}
                }
            ]
        }

        # Act
        states = self.tracker.list_all_states()

        # Assert
        # Should only include the state item, not the summary
        assert len(states) == 1
        assert "repo1" in states

        # Verify scan was called with correct filter
        call_kwargs = self.mock_dynamodb.scan.call_args[1]
        assert "FilterExpression" in call_kwargs
        assert call_kwargs["ExpressionAttributeValues"][":sk"]["S"] == "state#last-processed"

    def test_list_all_states_failure(self):
        """Test handling DynamoDB error when listing states."""
        # Arrange
        self.mock_dynamodb.scan.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "Scan"
        )

        # Act
        states = self.tracker.list_all_states()

        # Assert
        assert states == {}

    def test_save_and_get_state_roundtrip(self):
        """Test saving and retrieving state works correctly."""
        # Arrange
        repo = "test-repo"
        commit_sha = "abc123"
        files_processed = 150
        batches_sent = 30

        # Setup mock to return saved state
        saved_item = None

        def mock_put(TableName, Item):
            nonlocal saved_item
            saved_item = Item
            return {}

        def mock_get(TableName, Key):
            if saved_item:
                return {"Item": saved_item}
            return {}

        self.mock_dynamodb.put_item.side_effect = mock_put
        self.mock_dynamodb.get_item.side_effect = mock_get

        # Act
        save_result = self.tracker.save_state(repo, commit_sha, files_processed, batches_sent)
        retrieved_state = self.tracker.get_last_state(repo)

        # Assert
        assert save_result is True
        assert retrieved_state is not None
        assert retrieved_state["commit_sha"] == commit_sha
        assert retrieved_state["files_processed"] == files_processed
        assert retrieved_state["batches_sent"] == batches_sent
