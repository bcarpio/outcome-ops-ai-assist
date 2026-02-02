"""
Shared Module - Enterprise Component

This module contains shared utilities used across multiple Lambda functions.

What this module provides:
- S3 Vectors client wrapper for vector operations
- Bedrock embedding generation utilities
- SSM parameter caching
- Common error handling patterns
- Logging configuration

Components:
- s3_vectors.py: S3 Vectors client for PutVectors, QueryVectors, DeleteVectors
- embeddings.py: Bedrock Titan v2 embedding generation
- config.py: SSM parameter loading with caching
- logging.py: Structured logging configuration

Used by:
- query-kb (vector search)
- chat (conversation memory)
- ingest-docs (document embedding)
- generate-code-maps (code map embedding)
- process-batch-summary (batch embedding)
- process-repo-summary (repo embedding)

This module is available only via licensed deployments.

For enterprise briefings: https://www.outcomeops.ai
"""
