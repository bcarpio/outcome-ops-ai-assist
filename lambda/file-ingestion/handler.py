"""
File Ingestion Lambda for OutcomeOps AI Assist.

Central file ingestion service that:
1. Receives normalized messages from FILE_INGESTION_QUEUE
2. Routes to source-specific handlers (Confluence, Jira, Outlook, etc.)
3. Downloads/fetches content from source APIs
4. Uploads normalized files to S3 knowledge base bucket
5. S3 events then trigger process-documents for embedding

Message Format:
{
    "source": "confluence|jira|outlook|github",
    "workspace_id": "ws_xxx",
    "connection_id": "conn_...
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
