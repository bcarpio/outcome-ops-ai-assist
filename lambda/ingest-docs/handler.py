"""
Lambda handler for ingesting documentation into the knowledge base.

This handler:
1. Fetches ADRs, READMEs, and documentation files from GitHub repositories via GitHub API
2. Uploads them to S3 knowledge base bucket
3. Generates embeddings using Bedrock Titan Embeddings v2 (with smart text chunking for large files)
4. Stores embeddings and metadata in DynamoDB
5. Runs on EventBridge schedule (hourly)

Documentation sources:
- ADRs: docs/adr/*.md (from standards repos)
- READMEs: README.md, docs/README.md (from all repos)
- Docs: docs/*.md except ADRs and READMEs (from all repos)
  - This prevents chunking of large combined READMEs
  - Examples: docs/architecture.md, docs/lambda-*.md, docs/deployment.md
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# AWS clients (region inferred from environment or defaults to us-west-2)
s3_client = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
bedrock_client = boto3.client("bedrock-runtime")
ssm_client = boto3.client("ssm")

# Configuration (loaded from SSM at container startup)
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
REPO_NAME = os.environ.get("REPO_NAME", "outcome-ops-ai-assist")

KB_BUCKET = None
CODE_MAPS_TABLE = None
GITHUB_TOKEN = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global KB_BUCKET, CODE_MAPS_TABLE, GITHUB_TOKEN

    try:
        kb_bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3/knowledge-base-bucket"
        KB_BUCKET = ssm_client.get_parameter(Name=kb_bucket_param)["Parameter"]["Value"]

        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
        GITHUB_TOKEN = ssm_client.get_parameter(
            Name=github_token_param,
            WithDecryption=True
        )["Parameter"]["Value"]

        logger.info(f"Configuration loaded: KB_BUCKET={KB_BUCKET}, TABLE={CODE_MAPS_TABLE}")
    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
        raise


def github_api_request(endpoint: str, method: str = "GET") -> Dict[str, Any]:
    """
    Make a request to the GitHub API with authentication.

    Args:
        endpoint: API endpoint (without base URL)
        method: HTTP method (GET, POST, etc.)

    Returns:
        JSON response from GitHub API
    """
    url = f"{GITHUB_API_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "outcome-ops-ingest-docs",
    }

    try:
        request = Request(url, headers=headers, method=method)
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as e:
        logger.error(f"GitHub API error: {e}")
        raise


def github_api_raw_content(repo: str, path: str, ref: str = "main") -> str:
    """
    Fetch raw file content from GitHub.

    Args:
        repo: Repository in format "owner/repo"
        path: File path in repository
        ref: Branch/tag/commit (default: main)

    Returns:
        Raw file content as string
    """
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "outcome-ops-ingest-docs",
    }

    try:
        request = Request(url, headers=headers)
        with urlopen(request) as response:
            return response.read().decode("utf-8")
    except URLError as e:
        logger.error(f"Failed to fetch {path} from GitHub: {e}")
        raise


def list_directory_files(repo: str, directory: str, ref: str = "main") -> List[str]:
    """
    List all files in a directory using GitHub API.

    Args:
        repo: Repository in format "owner/repo"
        directory: Directory path
        ref: Branch/tag/commit (default: main)

    Returns:
        List of file paths
    """
    files = []
    endpoint = f"/repos/{repo}/contents/{directory}?ref={ref}"

    try:
        response = github_api_request(endpoint)

        if not isinstance(response, list):
            return files

        for item in response:
            if item["type"] == "file":
                files.append(item["path"])
            elif item["type"] == "dir":
                # Recursively list subdirectories
                subfiles = list_directory_files(repo, item["path"], ref)
                files.extend(subfiles)

        return files
    except Exception as e:
        logger.error(f"Failed to list files in {directory}: {e}")
        return files


def compute_content_hash(content: str) -> str:
    """
    Compute SHA-256 hash of content for change detection.

    Args:
        content: File content

    Returns:
        SHA-256 hash as hex string
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def chunk_text(text: str, max_tokens: int = 8000, overlap: int = 200) -> List[str]:
    """
    Split text into chunks that fit within token limits.

    Uses a simple word-based approach: ~1 token per 4 characters average.

    Args:
        text: Text to chunk
        max_tokens: Maximum tokens per chunk (default 8000, leaves buffer for Bedrock limit of 8192)
        overlap: Number of characters to overlap between chunks

    Returns:
        List of text chunks
    """
    # Rough estimate: 1 token ≈ 4 characters
    max_chars = max_tokens * 4

    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        # Take up to max_chars
        end = min(start + max_chars, len(text))

        # If we're not at the end, try to break at a paragraph boundary
        if end < len(text):
            # Look for last paragraph break
            last_para = text.rfind("\n\n", start, end)
            if last_para > start:
                end = last_para
            else:
                # Look for last sentence
                last_period = text.rfind(".", start, end)
                if last_period > start + max_chars * 0.7:  # At least 70% of max
                    end = last_period + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Move start position, accounting for overlap
        start = end - overlap
        if start >= len(text):
            break

    return chunks if chunks else [text]


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding for text using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed (will be chunked if too large)

    Returns:
        1024-dimensional embedding vector
    """
    try:
        payload = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v2:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload),
        )

        response_body = json.loads(response["body"].read())
        embedding = response_body.get("embedding", [])

        if not embedding:
            logger.warning(f"Empty embedding returned for text: {text[:100]}")
            return []

        logger.info(f"Generated embedding with {len(embedding)} dimensions")
        return embedding

    except ClientError as e:
        if "Too many input tokens" in str(e):
            logger.warning(f"Text too large ({len(text)} chars), chunking and using first chunk for embedding")
            chunks = chunk_text(text)
            if chunks:
                return generate_embedding(chunks[0])
            return []
        logger.error(f"Failed to generate embedding: {e}")
        raise


def upload_to_s3(file_key: str, content: str, file_path: str) -> bool:
    """
    Upload document to S3 knowledge base bucket.

    Args:
        file_key: S3 key (path in bucket)
        content: File content
        file_path: Original file path in repo

    Returns:
        True if successful, False otherwise
    """
    try:
        metadata = {
            "original-path": file_path,
            "ingested-at": datetime.utcnow().isoformat(),
        }

        s3_client.put_object(
            Bucket=KB_BUCKET,
            Key=file_key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
            Metadata=metadata,
        )

        logger.info(f"Uploaded document to S3: s3://{KB_BUCKET}/{file_key}")
        return True

    except ClientError as e:
        logger.error(f"Failed to upload to S3: {e}")
        return False


def store_in_dynamodb(
    pk: str, sk: str, doc_type: str, content: str, embedding: List[float],
    file_path: str, content_hash: str
) -> bool:
    """
    Store document metadata and embedding in DynamoDB.

    Args:
        pk: Partition key
        sk: Sort key
        doc_type: Document type (adr, readme)
        content: Document content
        embedding: 1024-dimensional embedding vector
        file_path: Original file path
        content_hash: SHA-256 hash of content

    Returns:
        True if successful, False otherwise
    """
    try:
        item = {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "type": {"S": doc_type},
            "content": {"S": content},
            "embedding": {"L": [{"N": str(x)} for x in embedding]},
            "file_path": {"S": file_path},
            "content_hash": {"S": content_hash},
            "timestamp": {"S": datetime.utcnow().isoformat()},
            "repo": {"S": REPO_NAME},
        }

        dynamodb_client.put_item(TableName=CODE_MAPS_TABLE, Item=item)

        logger.info(f"Stored in DynamoDB: PK={pk}, SK={sk}")
        return True

    except ClientError as e:
        logger.error(f"Failed to store in DynamoDB: {e}")
        return False


def ingest_adr(repo: str, adr_path: str, content: str) -> bool:
    """
    Ingest an ADR file.

    Args:
        repo: Repository name
        adr_path: Path to ADR file (e.g., docs/adr/ADR-001-...)
        content: File content

    Returns:
        True if successful
    """
    # Extract ADR ID from filename
    filename = adr_path.split("/")[-1]
    adr_id = filename.split(".")[0]  # e.g., "ADR-001"

    pk = f"repo#{REPO_NAME}"
    sk = f"adr#{adr_id}"
    doc_type = "adr"

    # Upload to S3
    s3_key = f"adr/{adr_id}.md"
    if not upload_to_s3(s3_key, content, adr_path):
        return False

    # Generate embedding
    embedding = generate_embedding(content)
    if not embedding:
        logger.error(f"Failed to generate embedding for ADR: {adr_id}")
        return False

    # Compute content hash
    content_hash = compute_content_hash(content)

    # Store in DynamoDB
    return store_in_dynamodb(pk, sk, doc_type, content, embedding, adr_path, content_hash)


def ingest_readme(repo: str, readme_path: str, content: str) -> bool:
    """
    Ingest a README file.

    Args:
        repo: Repository name
        readme_path: Path to README file
        content: File content

    Returns:
        True if successful
    """
    # Create identifier from path
    # e.g., "README.md" -> "root", "docs/README.md" -> "docs"
    path_parts = readme_path.split("/")
    if path_parts[0] == "README.md":
        readme_id = "root"
    else:
        readme_id = path_parts[0]

    pk = f"repo#{REPO_NAME}"
    sk = f"readme#{readme_id}"
    doc_type = "readme"

    # Upload to S3
    s3_key = f"readme/{readme_id}.md"
    if not upload_to_s3(s3_key, content, readme_path):
        return False

    # Generate embedding
    embedding = generate_embedding(content)
    if not embedding:
        logger.error(f"Failed to generate embedding for README: {readme_id}")
        return False

    # Compute content hash
    content_hash = compute_content_hash(content)

    # Store in DynamoDB
    return store_in_dynamodb(pk, sk, doc_type, content, embedding, readme_path, content_hash)


def ingest_doc(repo: str, doc_path: str, content: str) -> bool:
    """
    Ingest a generic documentation file from docs/ directory.

    Args:
        repo: Repository name
        doc_path: Path to doc file (e.g., docs/architecture.md)
        content: File content

    Returns:
        True if successful
    """
    # Extract doc identifier from filename
    # e.g., "docs/architecture.md" -> "architecture"
    # e.g., "docs/lambda-ingest-docs.md" -> "lambda-ingest-docs"
    filename = doc_path.split("/")[-1]
    doc_id = filename.split(".")[0]  # Remove .md extension

    pk = f"repo#{REPO_NAME}"
    sk = f"doc#{doc_id}"
    doc_type = "doc"

    # Upload to S3
    s3_key = f"docs/{doc_id}.md"
    if not upload_to_s3(s3_key, content, doc_path):
        return False

    # Generate embedding
    embedding = generate_embedding(content)
    if not embedding:
        logger.error(f"Failed to generate embedding for doc: {doc_id}")
        return False

    # Compute content hash
    content_hash = compute_content_hash(content)

    # Store in DynamoDB
    return store_in_dynamodb(pk, sk, doc_type, content, embedding, doc_path, content_hash)


def handler(event, context):
    """
    Lambda handler for ingesting documents.

    Triggered by EventBridge schedule (hourly) or manual invocation.
    """
    logger.info(f"Ingest handler invoked: {json.dumps(event)}")

    # Load configuration at container startup
    if KB_BUCKET is None:
        load_config()

    try:
        # Load allowlist from SSM Parameter Store
        # Users configure repos_to_ingest in tfvars, which gets stored in SSM by Terraform
        allowlist_param = f"/{ENVIRONMENT}/{APP_NAME}/config/repos-allowlist"
        try:
            param_response = ssm_client.get_parameter(Name=allowlist_param)
            allowlist = json.loads(param_response["Parameter"]["Value"])
        except ssm_client.exceptions.ParameterNotFound:
            logger.error(f"Repos allowlist not found in SSM at {allowlist_param}")
            logger.info("Make sure to deploy Terraform with repos_to_ingest configured in tfvars")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Repos allowlist not configured in SSM Parameter Store"}),
            }

        if not allowlist or "repos" not in allowlist:
            logger.error("Invalid allowlist structure in SSM Parameter Store")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Invalid allowlist structure"}),
            }

        total_docs_ingested = 0

        for repo_config in allowlist["repos"]:
            repo_name = repo_config["name"]
            repo_project = repo_config["project"]
            repo_type = repo_config.get("type", "internal")

            logger.info(f"Processing {repo_name} ({repo_project})...")

            try:
                # Fetch ADRs from standards repos
                if repo_type == "standards":
                    logger.info(f"Fetching ADRs from {repo_name}...")
                    adr_files = list_directory_files(repo_project, "docs/adr", ref="main")

                    for adr_file in adr_files:
                        if adr_file.endswith(".md"):
                            logger.info(f"Ingesting ADR: {adr_file}")
                            content = github_api_raw_content(repo_project, adr_file)
                            if ingest_adr(repo_name, adr_file, content):
                                total_docs_ingested += 1

                # Fetch READMEs from all repos
                logger.info(f"Fetching READMEs from {repo_name}...")
                readme_files = [
                    "README.md",  # Root README
                    "docs/README.md",  # Docs folder README
                ]

                for readme_file in readme_files:
                    try:
                        logger.info(f"Fetching {readme_file}...")
                        content = github_api_raw_content(repo_project, readme_file)
                        if ingest_readme(repo_name, readme_file, content):
                            total_docs_ingested += 1
                    except URLError:
                        logger.info(f"README not found: {readme_file}")
                        continue

                # Fetch all documentation files from docs/ directory
                logger.info(f"Fetching documentation files from {repo_name}...")
                try:
                    doc_files = list_directory_files(repo_project, "docs", ref="main")
                    # Filter out ADRs and READMEs (already handled separately)
                    doc_files = [
                        f for f in doc_files
                        if f.endswith(".md")
                        and "docs/adr" not in f  # Skip ADRs (handled above)
                        and not f.endswith("README.md")  # Skip READMEs (handled above)
                        and not f.endswith("TEMPLATE.md")  # Skip template
                    ]

                    for doc_file in doc_files:
                        try:
                            logger.info(f"Ingesting doc: {doc_file}")
                            content = github_api_raw_content(repo_project, doc_file)
                            if ingest_doc(repo_name, doc_file, content):
                                total_docs_ingested += 1
                        except URLError:
                            logger.error(f"Failed to fetch doc: {doc_file}")
                            continue
                except Exception as e:
                    logger.warning(f"Failed to list docs directory: {e}")
                    # Continue with other processing even if docs fetch fails

            except Exception as e:
                logger.error(f"Error processing {repo_name}: {e}")
                continue

        logger.info(f"Document ingestion completed: {total_docs_ingested} documents ingested")

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Document ingestion completed",
                    "documents_ingested": total_docs_ingested,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error during document ingestion: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
