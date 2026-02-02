"""
Vector Query - DEPRECATED

NOTE: This component has been superseded by native S3 Vectors integration.
Vector search is now performed directly within the query-kb Lambda using
AWS S3 Vectors native similarity search capabilities.

Historical context:
This Lambda previously performed semantic search by scanning DynamoDB and
calculating cosine similarity in Python. With the migration to S3 Vectors,
this functionality is now handled natively by AWS infrastructure, providing:
- 100-1000x faster query performance
- Native cosine similarity at the database level
- No client-side similarity calculations needed
- Better scalability for large knowledge bases

Current architecture:
- query-kb Lambda generates query embeddings via Bedrock Titan v2
- query-kb Lambda queries S3 Vectors directly (s3vectors:QueryVectors)
- S3 Vectors returns top-K results with native similarity scoring
- No separate vector-query Lambda invocation needed

This file is retained for reference and backwards compatibility documentation.

For enterprise briefings: https://www.outcomeops.ai
For questions: https://github.com/bcarpio/outcome-ops-ai-assist/discussions
"""


def handler(event, context):
    """
    DEPRECATED: This Lambda is no longer used in the current architecture.

    Vector search has been migrated to AWS S3 Vectors, which provides
    native similarity search without requiring a separate Lambda function.

    See query-kb Lambda for the current RAG pipeline implementation.
    """
    raise NotImplementedError(
        "DEPRECATED: Vector search is now performed via S3 Vectors. "
        "See query-kb Lambda for the current implementation."
    )
