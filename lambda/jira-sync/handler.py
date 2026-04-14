"""
Jira Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Jira issues:
1. Receives sync request from JIRA_SYNC_QUEUE
2. Lists issues in selected projects
3. Queues individual issues to FILE_INGESTION_QUEUE
4. Handles delta sync (only sync issues modified since last_sync_at)
5. Updates connection sync status

This is the orchestrator layer - actual issue fetching is done by file-ingestion.
"""


def handler(event, context):
    """
    Enterprise implementation placeholder.

    This function is part of the proprietary OutcomeOps platform.
    Available via enterprise licensing only.
    See: https://www.outcomeops.ai
    """
    raise NotImplementedError(
        "This is an enterprise component. "
        "Visit https://www.outcomeops.ai for deployment options."
    )
