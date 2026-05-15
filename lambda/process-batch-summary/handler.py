"""
Lambda handler for processing code map batch summaries from SQS.

This handler:
1. Receives batch messages from SQS FIFO queue
2. Fetches file contents from GitHub API
3. Generates batch summaries using Claude via Bedrock
4. Creates embeddings using Bedrock Titan v2
5. Stores summaries in DynamoDB with embeddings
6. Returns successfully processed records or raises errors for retry

Triggered by: SQS event source mapping
Documentation: docs/lambda-process-batch-summary.md
"""

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import COMPREHEND_MODEL_ID, log_audit_entry
from shared.audit_writer import ACTION_DOCUMENT_INGESTED, log_admin_action
from shared.pii import MODE_STRICT, read_workspace_mode, redact

# Symbol-graph extractor (plan-gkg chunk 01). The extractor emits typed
# Facts only — storage / SQS hand-off lands in chunks 02 and 03.
# Path-prepend below mirrors the ``shared`` import above so the extractor
# package resolves whether handler.py is loaded via the Lambda runtime
# (cwd=/var/task) or via importlib.util.spec_from_file_location in tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extractor import (  # type: ignore  # noqa: E402
    DEFAULT_SYMBOL_GRAPH_FILTER,
    Fact,
    SymbolGraphFilter,
    extract_facts_from_bytes,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Bedrock model IDs (configurable via environment variables)
ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# AWS clients (region inferred from environment)

# Configure boto3 with longer timeouts for Bedrock (Claude can take 2-3 minutes for large responses)
bedrock_config = Config(
    read_timeout=300,  # 5 minutes read timeout
    connect_timeout=10,
    retries={'max_attempts': 3}
)

dynamodb_client = boto3.client("dynamodb")
dynamodb_resource = boto3.resource("dynamodb")
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")


def _get_workspaces_table():
    """Lazy-init DDB table handle for workspace privacy_mode lookups."""
    name = os.environ.get("WORKSPACES_TABLE", "")
    if not name:
        return None
    return dynamodb_resource.Table(name)

# Configuration (loaded from SSM at container startup)
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
ENABLE_BEDROCK_COOLDOWN = os.environ.get("ENABLE_BEDROCK_COOLDOWN", "true").lower() == "true"

CODE_MAPS_TABLE = None
GITHUB_TOKEN = None
VECTOR_BUCKET = None
VECTOR_INDEX = None

GITHUB_API_URL = "https://api.github.com"


def load_config():
    """Load configuration from SSM Parameter Store at container startup."""
    global CODE_MAPS_TABLE, GITHUB_TOKEN, VECTOR_BUCKET, VECTOR_INDEX

    try:
        code_maps_param = f"/{ENVIRONMENT}/{APP_NAME}/dynamodb/code-maps-table"
        CODE_MAPS_TABLE = ssm_client.get_parameter(Name=code_maps_param)["Parameter"]["Value"]

        github_token_param = f"/{ENVIRONMENT}/{APP_NAME}/github/token"
        GITHUB_TOKEN = ssm_client.get_parameter(
            Name=github_token_param,
            WithDecryption=True
        )["Parameter"]["Value"]

        # S3 Vectors configuration
        vector_bucket_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/bucket-name"
        VECTOR_BUCKET = ssm_client.get_parameter(Name=vector_bucket_param)["Parameter"]["Value"]

        vector_index_param = f"/{ENVIRONMENT}/{APP_NAME}/s3vectors/index-name"
        VECTOR_INDEX = ssm_client.get_parameter(Name=vector_index_param)["Parameter"]["Value"]

        logger.info(f"Configuration loaded: VECTOR_BUCKET={VECTOR_BUCKET}")
    except ClientError as e:
        logger.error(f"Failed to load configuration from SSM: {e}")
        raise


# Cache for default branch per repo (persists within Lambda container)
_default_branch_cache: Dict[str, str] = {}


def get_default_branch(repo: str) -> str:
    """
    Get the default branch for a repository.

    Args:
        repo: Repository in format "owner/repo"

    Returns:
        Default branch name (e.g., "main" or "master")
    """
    if repo in _default_branch_cache:
        return _default_branch_cache[repo]

    url = f"{GITHUB_API_URL}/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "outcome-ops-process-batch-summary",
    }

    try:
        request = Request(url, headers=headers)
        with urlopen(request) as response:
            data = json.loads(response.read().decode("utf-8"))
            default_branch = data.get("default_branch", "main")
            _default_branch_cache[repo] = default_branch
            logger.info(f"Default branch for {repo}: {default_branch}")
            return default_branch
    except URLError as e:
        logger.warning(f"Failed to get default branch for {repo}, using 'main': {e}")
        return "main"


def fetch_file_content(repo: str, file_path: str) -> str:
    """
    Fetch raw file content from GitHub.

    Args:
        repo: Repository in format "owner/repo"
        file_path: Path to file in repository

    Returns:
        Raw file content as string
    """
    default_branch = get_default_branch(repo)
    # URL-encode the file path to handle special characters like # in ABAP filenames
    # (e.g., #dmo#cl_flight.clas.abap -> %23dmo%23cl_flight.clas.abap)
    encoded_path = quote(file_path, safe='/')
    url = f"https://raw.githubusercontent.com/{repo}/{default_branch}/{encoded_path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "outcome-ops-process-batch-summary",
    }

    try:
        request = Request(url, headers=headers)
        with urlopen(request) as response:
            return response.read().decode("utf-8")
    except URLError as e:
        logger.error(f"Failed to fetch {file_path} from GitHub: {e}")
        raise


def retry_bedrock_call(func, max_retries: int = 5):
    """
    Retry helper for Bedrock calls with exponential backoff.

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts (default 5)

    Returns:
        Result from successful function call
    """
    import time
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except ClientError as e:
            last_error = e
            error_code = e.response.get("Error", {}).get("Code", "")

            is_retryable = error_code in [
                "ThrottlingException",
                "ServiceUnavailableException",
                "InternalServerException"
            ]

            if not is_retryable or attempt == max_retries:
                raise

            # VERY aggressive exponential backoff for severe throttling
            # 10s, 20s, 40s, 80s, 120s = up to 2 minutes per retry
            delay_s = min(10 * (2 ** (attempt - 1)), 120)
            logger.warning(f"Bedrock throttled (attempt {attempt}/{max_retries}), waiting {delay_s}s before retry...")
            time.sleep(delay_s)

    raise last_error


# Constants for chunking
MAX_FILE_SIZE = 50000  # 50K chars - files larger than this get chunked
CHUNK_SIZE = 40000     # 40K chars per chunk (leaves room for overlap)
CHUNK_OVERLAP = 2000   # 2K char overlap between chunks for context

# Token limit - leave room for output tokens and template overhead
MAX_PROMPT_TOKENS = 150_000  # Bedrock limit is 200K; reserve 50K for output + safety


def estimate_tokens(text: str) -> int:
    """Estimate token count. Code averages ~3.5 chars/token; use 3 for safety margin."""
    return len(text) // 3


def chunk_large_content(content: str, file_path: str) -> List[Dict[str, Any]]:
    """
    Chunk large file content into manageable pieces.

    For files over MAX_FILE_SIZE, split into CHUNK_SIZE pieces with CHUNK_OVERLAP
    overlap between chunks to maintain context.

    Args:
        content: Full file content
        file_path: Path to file (for logging)

    Returns:
        List of chunk dicts with path, content, chunk_index, total_chunks
    """
    content_len = len(content)

    # Small enough to process as single chunk
    if content_len <= MAX_FILE_SIZE:
        return [{
            "path": file_path,
            "content": content[:10000],  # Still truncate display to 10KB
            "chunk_index": 0,
            "total_chunks": 1,
            "is_chunked": False
        }]

    # Need to chunk this file
    chunks = []
    chunk_index = 0
    position = 0

    while position < content_len:
        # Calculate chunk end position
        chunk_end = min(position + CHUNK_SIZE, content_len)

        # Extract chunk content
        chunk_content = content[position:chunk_end]

        chunks.append({
            "path": file_path,
            "content": chunk_content[:10000],  # Truncate display to 10KB per chunk
            "full_chunk": chunk_content,  # Keep full chunk for summarization
            "chunk_index": chunk_index,
            "start_char": position,
            "end_char": chunk_end,
            "is_chunked": True
        })

        chunk_index += 1
        # Move position forward, accounting for overlap
        position = chunk_end - CHUNK_OVERLAP if chunk_end < content_len else content_len

    # Set total_chunks on all chunks
    for chunk in chunks:
        chunk["total_chunks"] = len(chunks)

    logger.info(
        f"Chunked large file {file_path}: {content_len} chars -> {len(chunks)} chunks"
    )
    return chunks


def _format_file_content(fc: Dict[str, Any]) -> str:
    """Format a file content entry for the prompt."""
    if fc.get("is_chunked"):
        chunk_info = f" [Chunk {fc['chunk_index'] + 1}/{fc['total_chunks']}]"
        char_range = f" (chars {fc.get('start_char', 0)}-{fc.get('end_char', 0)})"
    else:
        chunk_info = ""
        char_range = ""

    truncated = "... (truncated)\n" if len(fc['content']) == 10000 else ""
    return f"File: {fc['path']}{chunk_info}{char_range}\n```\n{fc['content']}\n{truncated}```\n"


def _build_batch_prompt(
    batch_type: str, repo: str, group_name: str, files_section: str
) -> str:
    """Build prompt based on batch type with the given files section."""
    prompt_templates = {
        "infrastructure": f"Summarize the infrastructure for {repo} based on these Terraform files. Describe what resources are created, how they're organized, and the overall architecture.\n\n{files_section}",
        "handler-group": f"""Analyze the {group_name} Lambda handler for {repo} and provide detailed implementation documentation for debugging.

Extract the following for EACH function in this handler:

## Function Signatures
- Function name and parameters with types
- Parameter format specifications (e.g., UUID format, slug format: {{{{name[:90]}}}}-{{{{id[:6]}}}})
- Query string parameters (for API handlers) or event structure (for event-driven handlers)
- Required vs optional parameters

## Query Patterns
- DynamoDB query patterns (PK/SK keys, filters, scans, indexes)
- S3 access patterns (bucket, key patterns)
- External API calls and their purposes

## Returns and Errors
- Success responses with HTTP status codes (e.g., 200, 201)
- Error responses with HTTP status codes (e.g., 400, 404, 500)
- What triggers each error condition
- Response body structure for success and error cases

## Data Contracts
- Data format specifications (slug format, ID format, timestamp format, etc.)
- Where data is stored (DynamoDB table/fields, S3 paths, SSM parameters)
- Data relationships (e.g., slug derived from name + first 6 chars of ID)
- Field names and their types

## Common Pitfalls
- Incorrect parameter usage patterns that cause errors
- What will cause 404s (not found), 400s (bad request), or 500s (server errors)
- Incorrect assumptions about data formats
- Examples:
  - "❌ Passing UUID as 'slug' parameter → 404 not found"
  - "✅ Pass UUID as 'id' parameter OR actual slug as 'slug' parameter"
  - "❌ Assuming slug equals character_id"
  - "✅ Slug is derived from name + first 6 chars of ID"

## Cross-References
- Related functions in other handlers (with file paths)
- Shared utilities and their locations (e.g., jwt_helper.py, utils.py)
- Dependencies on other Lambdas or AWS services
- Where data transformations happen (e.g., slugify() function in character_management)

Format the output as structured documentation that helps developers debug issues and understand implementation details, not just high-level purpose.

{files_section}
""",
        # =========================================================================
        # Java-specific batch types (ADR-009 source modules)
        # =========================================================================
        "source-module": f"""Analyze the {group_name} Java module for {repo} and provide detailed implementation documentation.

Extract the following for EACH class in this module:

## Class Overview
- Class name, package, and purpose
- Inheritance hierarchy (@MappedSuperclass, extends, implements)
- Key annotations (@Entity, @Controller, @Service, @Repository, @Component, etc.)

## Fields and Properties
For each field:
- Field name and type
- Annotations (@Column, @Id, @GeneratedValue, @ManyToOne, @OneToMany, etc.)
- Validation constraints (@NotBlank, @Size, @Pattern, @Valid, etc.)
- Default values and nullability

## Method Signatures
For each public method:
- Method name, parameters with types, return type
- HTTP mapping annotations (@GetMapping, @PostMapping, path, params)
- Request/response body types (@RequestBody, @ResponseBody)
- Path variables (@PathVariable) and query parameters (@RequestParam)

## JPA/Hibernate Patterns
- Entity relationships (cascade types, fetch strategies, orphan removal)
- Query methods (Spring Data naming conventions, @Query annotations)
- Transaction boundaries (@Transactional)
- ID generation strategies

## Spring Patterns
- Dependency injection (@Autowired, constructor injection)
- Bean scopes (@Scope, @RequestScope)
- Exception handling (@ExceptionHandler, @ControllerAdvice)
- Security annotations (@PreAuthorize, @Secured)

## Validation Rules
| Field | Constraint | Error Message |
|-------|-----------|---------------|
- List all validation annotations and their error codes/messages

## Common Pitfalls
- Incorrect usage patterns that cause errors
- What triggers validation errors, 404s, or 500s
- N+1 query problems with lazy loading
- Transaction boundaries and rollback scenarios
- Examples:
  - "❌ Calling repository method outside @Transactional → LazyInitializationException"
  - "✅ Use @Transactional or fetch eagerly"

## Cross-References
- Related classes in other packages (with file paths)
- Shared utilities (validators, formatters, converters)
- Dependencies on other services or repositories

Format as structured documentation for developers debugging issues.

{files_section}
""",
        "build": f"""Analyze the build configuration for {repo} and document:

## Build Tool
- Maven (pom.xml) or Gradle (build.gradle/build.gradle.kts)
- Parent POM or build plugin versions

## Dependencies
List key dependencies with their purposes:
| Group ID | Artifact ID | Version | Purpose |
|----------|-------------|---------|---------|

## Plugins
- Build plugins and their configurations
- Test plugins (Surefire, Failsafe, JaCoCo)
- Packaging plugins (Spring Boot Maven plugin, etc.)

## Profiles
- Available Maven profiles or Gradle tasks
- Environment-specific configurations

## Module Structure
- Multi-module project structure (if applicable)
- Inter-module dependencies

{files_section}
""",
        "kubernetes": f"""Analyze the Kubernetes manifests for {repo} and document:

## Resources
For each manifest:
- Resource type (Deployment, Service, ConfigMap, Secret, Ingress, etc.)
- Resource name and namespace
- Key configuration (replicas, ports, environment variables)

## Deployment Configuration
- Container images and tags
- Resource limits and requests
- Probes (liveness, readiness, startup)
- Volume mounts and persistent storage

## Service Configuration
- Service types (ClusterIP, NodePort, LoadBalancer)
- Port mappings
- Selectors and labels

## Configuration Management
- ConfigMaps and their usage
- Secrets (without values) and their references
- Environment variable injection

## Networking
- Ingress rules and paths
- Network policies
- Service mesh configuration (if applicable)

{files_section}
""",
        # =========================================================================
        # TypeScript-specific batch types
        # =========================================================================
        "api-routes": f"""Analyze the {group_name} API routes for {repo} and provide detailed implementation documentation.

Extract the following for EACH route/endpoint:

## Route Definitions
- HTTP method (GET, POST, PUT, DELETE, PATCH)
- Route path with parameters (e.g., /users/:id, /api/v1/items)
- Middleware applied (authentication, validation, rate limiting)

## Request Format
- Path parameters with types and validation
- Query parameters with types and defaults
- Request body schema (TypeScript interface or Zod/Yup schema)
- Headers required (Authorization, Content-Type, custom headers)

## Response Format
- Success response structure with HTTP status codes
- Error response structure with error codes
- Pagination format (if applicable)

## Authentication & Authorization
- Auth middleware used (JWT, session, API key)
- Required roles or permissions
- Token/session handling

## Validation
- Request validation library (Zod, Yup, Joi, class-validator)
- Validation rules for each field
- Error messages returned on validation failure

## Database Operations
- ORM queries (Prisma, TypeORM, Drizzle, Knex)
- Raw SQL queries (if any)
- Transaction handling

## Common Pitfalls
- Invalid parameter formats that cause errors
- Missing required headers or body fields
- Authorization edge cases
- Examples:
  - "❌ Sending id as number when string UUID expected → 400 Bad Request"
  - "✅ Send id as string UUID format"

## Cross-References
- Shared middleware locations
- Related routes in other files
- Service layer dependencies

{files_section}
""",
        "frontend": f"""Analyze the {group_name} frontend components for {repo} and provide detailed documentation.

## Component Overview
For each component:
- Component name and purpose
- File path and export type (default/named)

## Props Interface
```typescript
// Document the exact TypeScript interface
interface ComponentProps {{
  // prop: type - description
}}
```
- Required vs optional props
- Default values
- Callback function signatures

## State Management
- Local state (useState, useReducer)
- Global state (Context, Redux, Zustand, Jotai)
- Server state (React Query, SWR, Apollo)

## Hooks Used
- Custom hooks and their purposes
- Side effects (useEffect) and their triggers
- Memoization (useMemo, useCallback)

## Event Handlers
- User interaction handlers (onClick, onChange, onSubmit)
- Event types and payloads
- Form handling patterns

## Styling
- Styling approach (CSS Modules, Tailwind, styled-components, etc.)
- Theme integration
- Responsive breakpoints

## Testing
- Test file location (if exists)
- Testing patterns (render, user events, mocks)

## Common Pitfalls
- Incorrect prop types that cause runtime errors
- Missing required props
- Stale closure issues in callbacks
- Examples:
  - "❌ Passing string to number prop → runtime error"
  - "✅ Parse string to number before passing"

## Cross-References
- Child components used
- Parent components that use this
- Shared hooks and utilities

{files_section}
""",
        "schemas": f"""Analyze the data schemas for {repo} and provide detailed documentation.

## Schema Definitions
For each schema/type:
- Schema name and purpose
- Validation library (Zod, Yup, Joi, io-ts, class-validator)

## Field Specifications
| Field | Type | Required | Validation | Default |
|-------|------|----------|------------|---------|
- List all fields with their constraints

## Validation Rules
- Custom validators and their logic
- Error messages for each validation rule
- Transformation/coercion rules

## Type Inference
- TypeScript types derived from schemas
- Runtime type guards

## Usage Patterns
- Where schemas are used (API validation, form validation, etc.)
- How to extend or compose schemas

{files_section}
""",
        "shared": f"""Analyze the {group_name} shared code for {repo} and provide detailed documentation.

## Utility Functions
For each function:
- Function name and purpose
- Parameter types and return type
- Side effects (if any)

## Function Signatures
```typescript
// Document exact signatures
function utilityName(param: Type): ReturnType
```

## Usage Examples
- Common usage patterns
- Edge cases and error handling

## Dependencies
- External libraries used
- Other internal utilities referenced

## Common Pitfalls
- Incorrect parameter usage
- Error conditions
- Thread safety / async considerations

{files_section}
""",
        "config": f"""Analyze the configuration files for {repo} and document:

## Configuration Files
For each config file:
- File name and purpose
- Key settings and their effects

## Build Configuration
- TypeScript settings (tsconfig.json)
- Bundler settings (vite, webpack, esbuild)
- Test configuration (jest, vitest)

## Deployment Configuration
- Serverless Framework settings
- Environment variables
- AWS/Cloud provider settings

## Development Tools
- Linting rules (ESLint)
- Formatting settings (Prettier)
- Git hooks (Husky, lint-staged)

{files_section}
""",
        # =========================================================================
        # Common batch types (shared across backends)
        # =========================================================================
        "frontend-pages": f"Summarize the pages/routes for {repo}. Describe what pages exist, their routes, key features, state management patterns, and how they connect to the overall application.\n\n{files_section}",
        "frontend-components": f"Summarize the React components for {repo}. Describe what UI components are provided, their props/interfaces, reusability patterns, and styling approaches.\n\n{files_section}",
        "frontend-tests": f"Summarize the frontend tests for {repo}. Describe what components/features are tested, testing library used (Jest, Vitest, etc.), and testing patterns.\n\n{files_section}",
        "frontend-utils": f"Summarize the frontend utilities and hooks for {repo}. Describe what helper functions, custom hooks, and context providers are available and how they're used.\n\n{files_section}",
        "frontend-types": f"Summarize the TypeScript types and interfaces for {repo}. Describe what data structures, API types, and component prop types are defined.\n\n{files_section}",
        "tests": f"""Analyze the {group_name} tests for {repo} and document:

## Test Overview
- Testing framework (JUnit, Jest, pytest, etc.)
- Test runner and configuration
- Coverage approach

## Test Classes/Files
For each test file:
- What is being tested (class, function, feature)
- Test setup (@BeforeEach, beforeAll, fixtures)
- Mocking strategy (Mockito, Jest mocks, etc.)

## Test Patterns
- Unit test patterns
- Integration test patterns
- Test data builders/factories

## Key Test Cases
- Critical functionality covered
- Edge cases tested
- Error scenarios validated

{files_section}
""",
        "docs": f"Summarize the documentation for {repo}. Describe what topics are covered and how the documentation is organized.\n\n{files_section}",
    }

    return prompt_templates.get(
        batch_type,
        f"Summarize these {group_name} files for {repo}.\n\n{files_section}"
    )


def _call_bedrock_and_audit(prompt: str, group_name: str) -> str:
    """Call Bedrock with the given prompt and log audit entry. Returns summary text."""
    def call_claude():
        payload = {
            "modelId": ADVANCED_MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            "inferenceConfig": {
                "maxTokens": 8192,
                "temperature": 0.3,
            },
        }

        response = bedrock_client.converse(**payload)

        output_message = response.get("output", {}).get("message", {})
        content = output_message.get("content", [])

        if not content or not content[0].get("text"):
            raise ValueError("No content in Claude response")

        return content[0]["text"], response.get("usage", {})

    bedrock_start = time.time()
    summary, usage = retry_bedrock_call(call_claude)
    bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)
    logger.info(f"Generated batch summary for {group_name} ({len(summary)} chars)")

    log_audit_entry(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        lambda_name="process-batch-summary",
        model_id=ADVANCED_MODEL_ID,
        input_text=prompt,
        output_text=summary,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        duration_ms=bedrock_duration_ms,
    )

    return summary


def generate_batch_summary(
    batch: Dict[str, Any],
    fact_sink: "list[Fact] | None" = None,
    sg_filter: SymbolGraphFilter = DEFAULT_SYMBOL_GRAPH_FILTER,
) -> str:
    """
    Generate batch summary using Claude via Bedrock.

    If the assembled prompt exceeds MAX_PROMPT_TOKENS, the file contents are
    split into sub-batches. Each sub-batch is summarized independently, then
    the sub-summaries are merged into a single coherent summary.

    Args:
        batch: Batch message containing repo, batch_type, group_name, file_paths
        fact_sink: Optional list to append symbol-graph Facts to. The
            extractor (plan-gkg chunk 01) runs on the raw fetched bytes
            BEFORE chunking so symbol resolution sees complete files.
            When ``None`` the extractor pass is skipped — keeps the
            existing call sites (tests, legacy paths) unchanged.
        sg_filter: Per-repo symbol-graph extraction filter (plan-gkg
            chunk 11). Applied per-file inside ``_iter_batch_facts``.
            Default is the permissive module-level filter, so legacy
            call sites keep their current behaviour.

    Returns:
        Generated summary text
    """
    repo = batch["repo"]
    repo_project = batch["repo_project"]
    batch_type = batch["batch_type"]
    group_name = batch["group_name"]
    file_paths = batch["file_paths"]

    # Fetch all file contents
    file_contents = []

    for file_path in file_paths:
        try:
            content = fetch_file_content(repo_project, file_path)

            # Symbol-graph extractor pass (plan-gkg chunk 03). Run on
            # the FULL raw content, not the chunked / truncated copy —
            # truncation would drop symbol definitions and produce
            # spurious "no callers" results. Best-effort, per-file
            # try/except: an extractor crash never blocks the summary.
            if fact_sink is not None:
                language = _language_for_path(file_path)
                if language is not None:
                    fact_sink.extend(
                        _iter_batch_facts(
                            file_path, language, content, sg_filter
                        )
                    )

            # Chunk large files instead of skipping them
            chunks = chunk_large_content(content, file_path)
            file_contents.extend(chunks)

        except Exception as e:
            logger.error(f"Error fetching file {file_path}: {e}")
            # Continue with other files

    if not file_contents:
        return "No files available for analysis"

    # Build full prompt and check token estimate
    files_section = "\n".join([_format_file_content(fc) for fc in file_contents])
    prompt = _build_batch_prompt(batch_type, repo, group_name, files_section)
    estimated_tokens = estimate_tokens(prompt)

    if estimated_tokens <= MAX_PROMPT_TOKENS:
        return _call_bedrock_and_audit(prompt, group_name)

    # Batch too large - split file_contents into sub-batches
    # Calculate token budget for file content by subtracting template overhead
    template_token_overhead = estimated_tokens - estimate_tokens(files_section)
    file_token_budget = MAX_PROMPT_TOKENS - template_token_overhead

    sub_batches = []
    current_sub = []
    current_tokens = 0

    for fc in file_contents:
        fc_text = _format_file_content(fc)
        fc_tokens = estimate_tokens(fc_text)
        if current_sub and current_tokens + fc_tokens > file_token_budget:
            sub_batches.append(current_sub)
            current_sub = []
            current_tokens = 0
        current_sub.append(fc)
        current_tokens += fc_tokens

    if current_sub:
        sub_batches.append(current_sub)

    logger.info(
        f"Batch too large ({estimated_tokens} estimated tokens), "
        f"splitting into {len(sub_batches)} sub-batches"
    )

    # Summarize each sub-batch
    sub_summaries = []
    for i, sub_fc_list in enumerate(sub_batches, 1):
        logger.info(f"Processing sub-batch {i}/{len(sub_batches)} ({len(sub_fc_list)} file chunks)")
        sub_files_section = "\n".join([_format_file_content(fc) for fc in sub_fc_list])
        sub_prompt = _build_batch_prompt(batch_type, repo, group_name, sub_files_section)
        sub_summary = _call_bedrock_and_audit(
            sub_prompt, f"{group_name} (sub-batch {i}/{len(sub_batches)})"
        )
        sub_summaries.append(sub_summary)

    # Merge sub-summaries into a single coherent summary
    numbered_summaries = "\n\n".join(
        f"--- Sub-summary {i}/{len(sub_summaries)} ---\n{ss}"
        for i, ss in enumerate(sub_summaries, 1)
    )
    merge_prompt = (
        f"You are merging {len(sub_summaries)} partial summaries of the "
        f"{group_name} ({batch_type}) for {repo} into a single coherent summary. "
        f"Combine all information, remove redundancy, and produce a unified document "
        f"that reads as if it was written from a single analysis pass.\n\n"
        f"{numbered_summaries}"
    )

    return _call_bedrock_and_audit(merge_prompt, f"{group_name} (merge)")


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed

    Returns:
        1024-dimensional embedding vector
    """
    def call_titan():
        payload = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        response = bedrock_client.invoke_model(
            modelId=EMBEDDING_MODEL_ID,
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

    return retry_bedrock_call(call_titan)


def compute_content_hash(content: str) -> str:
    """
    Compute SHA-256 hash of content for change detection.

    Args:
        content: Content to hash

    Returns:
        SHA-256 hash as hex string
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_batch_summary(
    batch: Dict[str, Any],
    summary: str,
    embedding: List[float]
) -> bool:
    """
    Store batch summary in S3 Vectors.

    Vector key format:
    - Workspace repos: {workspace_id}#github#{repo}#{batch_type}-summary#{group_name}
    - Legacy repos: {repo}#{batch_type}-summary#{group_name}

    Args:
        batch: Batch message with metadata (may include workspace_id)
        summary: Generated summary text
        embedding: Embedding vector

    Returns:
        True if successful, False otherwise
    """
    try:
        content_hash = compute_content_hash(summary)
        doc_type = f"{batch['batch_type']}-summary"
        workspace_id = batch.get("workspace_id")

        # Construct vector key
        # Workspace repos: {workspace_id}#github#{repo}#{doc_type}#{group_name}
        # Legacy repos: {repo}#{doc_type}#{group_name}
        if workspace_id:
            vector_key = f"{workspace_id}#github#{batch['repo']}#{doc_type}#{batch['group_name']}"
        else:
            vector_key = f"{batch['repo']}#{doc_type}#{batch['group_name']}"

        metadata = {
            "content": summary,
            "content_hash": content_hash,
            "repo": batch["repo_project"],
            "type": doc_type,
            "batch_type": batch["batch_type"],
            "group_name": batch["group_name"],
            "file_count": str(len(batch["file_paths"])),
            "timestamp": datetime.utcnow().isoformat()
        }
        if workspace_id:
            metadata["workspace_id"] = workspace_id
            metadata["source"] = "github"

        s3vectors_client.put_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            vectors=[{
                "key": vector_key,
                "data": {"float32": embedding},
                "metadata": metadata
            }]
        )

        logger.info(f"Stored {batch['batch_type']} summary for {batch['group_name']} in S3 Vectors: {vector_key}")
        return True

    except ClientError as e:
        logger.error(f"Failed to store batch summary in S3 Vectors: {e}")
        return False


def _emit_ingest_audit(
    workspace_id: str,
    source: str,
    document_id: str,
    redaction,
    mode: str,
) -> None:
    """Emit two audit rows for one ingest: an admin DOCUMENT_INGESTED
    row that surfaces in the audit dashboard, and a Comprehend bedrock
    row that rolls into cost-by-model. Skipped runs (workspace_none /
    org_disabled) emit only the admin row. Non-fatal."""
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return

    log_admin_action(
        audit_table,
        ACTION_DOCUMENT_INGESTED,
        workspace_id=workspace_id,
        user_email="",  # SQS-driven; no user context.
        source_ip="",
        user_agent="",
        details={
            "source": source,
            "document_id": document_id,
            "char_count": redaction.char_count,
            "redaction_summary": redaction.summary,
            "skipped_reason": redaction.skipped_reason,
            "mode": mode,
        },
    )

    if redaction.skipped_reason in ("", "comprehend_error"):
        log_audit_entry(
            audit_table_name=audit_table,
            lambda_name="process-batch-summary",
            model_id=COMPREHEND_MODEL_ID,
            input_text="",
            output_text="",
            char_count=redaction.char_count,
            workspace_id=workspace_id,
        )


# Map a repo-relative file path to the symbol-graph extractor language.
# Returns ``None`` when the extension is unknown — the extractor pass
# is best-effort and any unrecognized file is silently skipped.
_LANGUAGE_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".abap": "abap",
}


def _language_for_path(file_path: str) -> str | None:
    lower = file_path.lower()
    for suffix, language in _LANGUAGE_BY_EXT.items():
        if lower.endswith(suffix):
            return language
    return None


def _emit_facts(
    facts: Iterable[Fact],
    *,
    workspace_id: str = "",
    repo: str = "",
    commit_sha: str = "",
    previous_graph_sha: "str | None" = None,
) -> int:
    """Hand the Fact stream to the symbol-graph writer (plan-gkg chunks 02/03).

    When ``workspace_id`` / ``repo`` / ``commit_sha`` are all present the
    Facts are persisted via the delete-then-write path in
    ``shared.symbol_graph_writer``. The chunk-03 pointer state machine
    in ``shared.symbol_graph_pointer`` owns the actual ``graph_sha``
    advance, so the writer is invoked with ``skip_pointer_swap=True``
    to keep the legacy ``current_commit_sha`` write from clobbering
    the new ``graph_sha`` / ``graph_pending_sha`` attributes on the
    pointer row.

    Args:
        facts: Iterable of chunk-01 Facts.
        workspace_id / repo / commit_sha: Storage context. When any are
            missing the call is a no-op log (legacy producer path).
        previous_graph_sha: Caller-supplied previous SHA from the
            chunk-03 pointer module. Bypasses the writer's legacy
            pointer read so it doesn't reach for a code-maps row that
            no longer carries the source-of-truth pointer.

    Returns the number of Facts the writer accepted (zero when storage
    was skipped). Failures are caught and logged; the symbol-graph regen
    must never break the existing batch-summary path.
    """

    materialized: list[Fact] = list(facts)
    count = len(materialized)

    if not (workspace_id and repo and commit_sha):
        logger.info(
            "symbol-graph extractor emitted %d facts (storage skipped — "
            "no workspace/commit context on the SQS message)",
            count,
        )
        return 0

    try:
        from shared.symbol_graph_writer import write_facts  # type: ignore

        result = write_facts(
            workspace_id=workspace_id,
            repo=repo,
            commit_sha=commit_sha,
            facts=materialized,
            previous_commit_sha=previous_graph_sha or "",
            skip_pointer_swap=True,
        )
        logger.info(
            "symbol-graph wrote %d facts (deleted %d) for %s/%s@%s",
            result.facts_written,
            result.facts_deleted,
            workspace_id,
            repo,
            commit_sha,
        )
        return result.facts_written
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "symbol-graph write failed for %s/%s@%s: %s",
            workspace_id,
            repo,
            commit_sha,
            exc,
        )
        return 0


def _iter_batch_facts(
    file_path: str,
    language: str,
    content: str,
    config: SymbolGraphFilter = DEFAULT_SYMBOL_GRAPH_FILTER,
) -> Iterator[Fact]:
    """Yield Facts for a single file that already has its content in hand.

    ``config`` is the per-repo extraction filter (plan-gkg chunk 11);
    when omitted the permissive default is used so legacy call sites
    keep their current behavior.
    """

    try:
        yield from extract_facts_from_bytes(
            file_path, language, content.encode("utf-8"), config=config
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("extractor failed on %s: %s", file_path, exc)


def _load_symbol_graph_filter(
    repo_full_name: str, branch_name: str, github_token: str
) -> SymbolGraphFilter:
    """Load the per-repo symbol-graph extraction filter from
    ``.outcomeops.yaml`` (plan-gkg chunk 11).

    Reads ``ingestion.symbol_graph`` from the canonical Pydantic-validated
    ``RepoConfig`` (``lambda/shared/repo_config.py``) and maps the values
    onto the internal ``SymbolGraphFilter`` dataclass that the extractor
    package consumes.

    Failure modes are deliberately quiet: any error (missing file,
    network blip, malformed YAML, Pydantic ValidationError) is logged
    and the default permissive filter is returned. The symbol-graph
    pipeline MUST NEVER block the batch-summary path — degraded
    behaviour here means "extract everything we'd extract without the
    config", not "crash the summary".
    """

    try:
        # Import inside the function so the shared module path-prepend
        # (already done at the top of handler.py) is in effect.
        from shared.repo_config import (  # type: ignore  # noqa: E402
            load_repo_config_from_github,
        )

        cfg = load_repo_config_from_github(
            repo_full_name, branch_name, github_token
        )
        if cfg is None:
            return DEFAULT_SYMBOL_GRAPH_FILTER
        sg = cfg.ingestion.symbol_graph
        return SymbolGraphFilter(
            enabled=sg.enabled,
            include=list(sg.include),
            exclude=list(sg.exclude),
            max_file_bytes=sg.max_file_bytes,
            languages=list(sg.languages) if sg.languages is not None else None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "symbol-graph config load failed for %s@%s: %s — "
            "using default permissive filter",
            repo_full_name,
            branch_name,
            exc,
        )
        return DEFAULT_SYMBOL_GRAPH_FILTER


def process_batch_record(record: Dict[str, Any]) -> None:
    """
    Process a single SQS record.

    Args:
        record: SQS record containing batch message

    Raises:
        Exception: If processing fails (triggers SQS retry)
    """
    import time

    batch = json.loads(record["body"])

    logger.info(
        f"Processing {batch['batch_type']} batch: {batch['group_name']} "
        f"({len(batch['file_paths'])} files)"
    )

    # Symbol-graph regen context (plan-gkg chunk 03). All three of
    # workspace_id, repo, and commit_sha must be present for the
    # atomic-swap path to engage; legacy producers that omit any of
    # them fall through to the existing summary-only flow.
    sg_workspace = batch.get("workspace_id") or ""
    sg_repo = batch.get("repo") or ""
    sg_commit_sha = batch.get("commit_sha") or ""
    sg_enabled = bool(sg_workspace and sg_repo and sg_commit_sha)

    # Begin the atomic swap: claim graph_pending_sha on the pointer row.
    # Failure here means a concurrent regen owns the pointer; we still
    # proceed with the LLM summary path (legacy contract) but skip the
    # symbol-graph writer to avoid stomping the other regen.
    sg_facts: "list[Fact]" = []
    sg_previous_graph_sha: "str | None" = None
    sg_claimed = False
    if sg_enabled:
        try:
            from shared.symbol_graph_pointer import (  # type: ignore
                begin_swap,
                get_previous_graph_sha,
            )

            sg_previous_graph_sha = get_previous_graph_sha(
                sg_workspace, sg_repo, code_maps_table_name=CODE_MAPS_TABLE
            )
            sg_claimed = begin_swap(
                sg_workspace,
                sg_repo,
                sg_commit_sha,
                code_maps_table_name=CODE_MAPS_TABLE,
            )
            if not sg_claimed:
                logger.warning(
                    "symbol-graph regen for %s/%s@%s blocked — "
                    "concurrent regen owns the pointer; skipping write",
                    sg_workspace,
                    sg_repo,
                    sg_commit_sha,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "symbol-graph begin_swap failed for %s/%s@%s: %s — "
                "continuing without graph regen",
                sg_workspace,
                sg_repo,
                sg_commit_sha,
                exc,
            )
            sg_claimed = False

    # Generate batch summary. When the symbol-graph claim succeeded we
    # also collect Facts inline (the extractor sees raw bytes BEFORE
    # the summary path's chunk/truncate, so symbol resolution is
    # complete). Any extractor failure is per-file try/except inside
    # ``_iter_batch_facts`` — never blocks the summary.
    #
    # Per-repo .outcomeops.yaml ``ingestion.symbol_graph`` knobs
    # (plan-gkg chunk 11) only matter on the extraction path, so the
    # filter is only loaded when we've already claimed the swap. On the
    # legacy / sg-disabled path the filter is irrelevant.
    try:
        if sg_claimed:
            sg_branch = get_default_branch(sg_repo)
            sg_filter = _load_symbol_graph_filter(
                sg_repo, sg_branch, GITHUB_TOKEN or ""
            )
            batch_summary = generate_batch_summary(
                batch, fact_sink=sg_facts, sg_filter=sg_filter
            )
        else:
            batch_summary = generate_batch_summary(batch)
    except Exception:
        # Summary path failed — abandon the pending swap so the next
        # regen can claim the pointer cleanly.
        if sg_claimed:
            _abandon_symbol_graph_swap(sg_workspace, sg_repo)
        raise

    # PII redaction (plan/ chunk 05). Code summaries occasionally include
    # accidentally-pasted credentials / customer data from inline
    # comments; redact before embedding so neither S3 Vectors nor the
    # downstream RAG flow sees raw identifiers. Honors the workspace's
    # privacy_mode and the org-level kill switch.
    try:
        workspace_id = batch.get("workspace_id") or ""
        if workspace_id:
            ws_table = _get_workspaces_table()
            org_enabled = (
                os.environ.get("PII_REDACTION_ENABLED", "true").lower() == "true"
            )
            mode = (
                read_workspace_mode(ws_table, workspace_id)
                if ws_table is not None
                else MODE_STRICT
            )
            redaction = redact(batch_summary, mode=mode, org_enabled=org_enabled)
            batch_summary = redaction.text
            _emit_ingest_audit(
                workspace_id=workspace_id,
                source="github",
                document_id=f"{batch['repo']}#{batch['group_name']}",
                redaction=redaction,
                mode=mode,
            )

        # Generate embedding for batch summary
        batch_embedding = generate_embedding(batch_summary)

        # Store batch summary
        if not store_batch_summary(batch, batch_summary, batch_embedding):
            raise Exception(f"Failed to store batch summary for {batch['group_name']}")
    except Exception:
        # Any failure after the summary was generated still leaves the
        # symbol-graph pending swap open. Abandon it so the next regen
        # gets a clean pointer; the SQS retry will redo the work.
        if sg_claimed:
            _abandon_symbol_graph_swap(sg_workspace, sg_repo)
        raise

    # Symbol-graph write + atomic-swap commit (plan-gkg chunk 03).
    # Runs AFTER the summary path has fully succeeded so a regen never
    # leaves a fresh graph slice attached to a half-written summary.
    if sg_claimed:
        try:
            _emit_facts(
                iter(sg_facts),
                workspace_id=sg_workspace,
                repo=sg_repo,
                commit_sha=sg_commit_sha,
                previous_graph_sha=sg_previous_graph_sha,
            )
            _commit_symbol_graph_swap(sg_workspace, sg_repo, sg_commit_sha)
        except Exception as exc:
            # Write or commit failed — abandon the pending swap. The
            # already-written facts under the new SHA become orphans
            # that the next regen will overwrite (idempotent SK) or
            # the weekly sweep will GC. The pointer remains on the
            # previous graph_sha so readers see consistent state.
            logger.warning(
                "symbol-graph write/commit failed for %s/%s@%s: %s — "
                "abandoning pending swap",
                sg_workspace,
                sg_repo,
                sg_commit_sha,
                exc,
            )
            _abandon_symbol_graph_swap(sg_workspace, sg_repo)

    logger.info(f"Successfully processed {batch['batch_type']} summary for {batch['group_name']}")

    # Optional cooldown period to prevent Bedrock throttling on next message
    # Useful for accounts with low Bedrock quotas; disable for enterprise accounts
    if ENABLE_BEDROCK_COOLDOWN:
        cooldown_seconds = 10
        logger.info(f"Waiting {cooldown_seconds}s cooldown before next message...")
        time.sleep(cooldown_seconds)


def _commit_symbol_graph_swap(
    workspace_id: str, repo: str, commit_sha: str
) -> None:
    """Best-effort wrapper around ``symbol_graph_pointer.commit_swap``.

    Raises on the underlying ClientError so the caller can abandon the
    pending swap — a conditional-check failure (pending sha changed
    out from under us) returns False here and is left for the caller
    to react to.
    """
    from shared.symbol_graph_pointer import commit_swap  # type: ignore

    ok = commit_swap(
        workspace_id,
        repo,
        commit_sha,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    if not ok:
        raise RuntimeError(
            f"symbol-graph commit_swap rejected for {workspace_id}/{repo}@{commit_sha}"
        )


def _abandon_symbol_graph_swap(workspace_id: str, repo: str) -> None:
    """Best-effort wrapper around ``symbol_graph_pointer.abandon_swap``.

    Never raises — the abandon path runs from failure handlers and
    must not mask the original exception.
    """
    try:
        from shared.symbol_graph_pointer import abandon_swap  # type: ignore

        abandon_swap(
            workspace_id, repo, code_maps_table_name=CODE_MAPS_TABLE
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "symbol-graph abandon_swap failed for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )


def handler(event, context):
    """
    Lambda handler for processing batch summaries from SQS.

    Args:
        event: SQS event with Records array
        context: Lambda context

    Returns:
        Success response or raises exception for retry
    """
    logger.info(f"Process batch summary handler invoked with {len(event['Records'])} records")

    # Load configuration at container startup
    if CODE_MAPS_TABLE is None:
        load_config()

    errors = []

    for record in event["Records"]:
        try:
            process_batch_record(record)
        except Exception as e:
            logger.error(f"Error processing batch: {e}", exc_info=True)
            errors.append(e)
            # Continue processing other records

    # If any errors occurred, throw to trigger SQS retry
    if errors:
        raise Exception(
            f"Failed to process {len(errors)} out of {len(event['Records'])} batches"
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Batch processing completed",
            "records_processed": len(event["Records"]),
        }),
    }
