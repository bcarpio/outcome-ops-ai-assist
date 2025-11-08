"""
State tracking for incremental code map generation.

Tracks last processed commit SHA per repository to enable incremental updates.
State is stored in DynamoDB for persistence across Lambda invocations.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class StateTracker:
    """
    Tracks processing state for code map generation.

    Stores last processed commit SHA and timestamp in DynamoDB for each repository.
    """

    def __init__(self, dynamodb_client, table_name: str):
        """
        Initialize state tracker.

        Args:
            dynamodb_client: boto3 DynamoDB client
            table_name: DynamoDB table name for code maps
        """
        self.dynamodb_client = dynamodb_client
        self.table_name = table_name

    def get_last_state(self, repo: str) -> Optional[Dict[str, Any]]:
        """
        Get last processing state for a repository.

        Args:
            repo: Repository name

        Returns:
            State dictionary with commit_sha and timestamp, or None if not found

        Example:
            {
                "commit_sha": "abc123def456...",
                "timestamp": "2025-01-15T10:00:00.000Z",
                "files_processed": 150,
                "batches_sent": 25
            }
        """
        try:
            response = self.dynamodb_client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": f"repo#{repo}"},
                    "SK": {"S": "state#last-processed"},
                }
            )

            if "Item" not in response:
                logger.info(f"No previous state found for {repo}")
                return None

            item = response["Item"]
            state = {
                "commit_sha": item.get("commit_sha", {}).get("S"),
                "timestamp": item.get("timestamp", {}).get("S"),
                "files_processed": int(item.get("files_processed", {}).get("N", 0)),
                "batches_sent": int(item.get("batches_sent", {}).get("N", 0)),
            }

            logger.info(f"Retrieved last state for {repo}: {state['commit_sha'][:7]} at {state['timestamp']}")
            return state

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                logger.warning(f"DynamoDB table not found: {self.table_name}")
                return None
            logger.error(f"Failed to get last state for {repo}: {e}")
            return None

    def save_state(
        self,
        repo: str,
        commit_sha: str,
        files_processed: int = 0,
        batches_sent: int = 0
    ) -> bool:
        """
        Save processing state for a repository.

        Args:
            repo: Repository name
            commit_sha: Current commit SHA processed
            files_processed: Number of files processed
            batches_sent: Number of batches sent to SQS

        Returns:
            True if successful, False otherwise
        """
        try:
            item = {
                "PK": {"S": f"repo#{repo}"},
                "SK": {"S": "state#last-processed"},
                "commit_sha": {"S": commit_sha},
                "timestamp": {"S": datetime.utcnow().isoformat()},
                "files_processed": {"N": str(files_processed)},
                "batches_sent": {"N": str(batches_sent)},
            }

            self.dynamodb_client.put_item(
                TableName=self.table_name,
                Item=item
            )

            logger.info(f"Saved state for {repo}: {commit_sha[:7]} ({files_processed} files, {batches_sent} batches)")
            return True

        except ClientError as e:
            logger.error(f"Failed to save state for {repo}: {e}")
            return False

    def clear_state(self, repo: str) -> bool:
        """
        Clear processing state for a repository.

        Useful for forcing full regeneration on next run.

        Args:
            repo: Repository name

        Returns:
            True if successful, False otherwise
        """
        try:
            self.dynamodb_client.delete_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": f"repo#{repo}"},
                    "SK": {"S": "state#last-processed"},
                }
            )

            logger.info(f"Cleared state for {repo}")
            return True

        except ClientError as e:
            logger.error(f"Failed to clear state for {repo}: {e}")
            return False

    def list_all_states(self) -> Dict[str, Dict[str, Any]]:
        """
        List processing state for all repositories.

        Returns:
            Dictionary mapping repo name to state

        Example:
            {
                "outcome-ops-ai-assist": {
                    "commit_sha": "abc123...",
                    "timestamp": "2025-01-15T10:00:00.000Z",
                    ...
                },
                "fantacyai-api-aws": { ... }
            }
        """
        states = {}

        try:
            # Query all state records
            response = self.dynamodb_client.scan(
                TableName=self.table_name,
                FilterExpression="begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":sk": {"S": "state#last-processed"}
                }
            )

            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk.startswith("repo#"):
                    repo = pk.replace("repo#", "")
                    states[repo] = {
                        "commit_sha": item.get("commit_sha", {}).get("S"),
                        "timestamp": item.get("timestamp", {}).get("S"),
                        "files_processed": int(item.get("files_processed", {}).get("N", 0)),
                        "batches_sent": int(item.get("batches_sent", {}).get("N", 0)),
                    }

            logger.info(f"Retrieved state for {len(states)} repositories")
            return states

        except ClientError as e:
            logger.error(f"Failed to list all states: {e}")
            return {}
