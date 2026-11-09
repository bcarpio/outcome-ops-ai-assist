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
from typing import Any, Dict, Iterable, Iterator, List, Optional
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
from shared.code_map_sizing import MAX_PROMPT_TOKENS, estimate_tokens
from shared.model_client import invoke as invoke_model
from shared.model_client import resolve_profile_arn
from shared.pii import MODE_STRICT, read_workspace_mode, redact
from shared.prompts import CODE_MAP_LITERAL_DISCIPLINE

# Symbol-graph extractor. The extractor emits typed Facts only —
# storage and the SQS hand-off are handled downstream.
# Path-prepend below mirrors the ``shared`` import above so the extractor
# package resolves whether handler.py is loaded via the Lambda runtime
# (cwd=/var/task) or via importlib.util.spec_from_file_location in tests.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extractor import (  # type: ignore  # noqa: E402
    DEFAULT_SYMBOL_GRAPH_FILTER,
    Fact,
    SymbolGraphFilter,
    extract_facts_from_bytes,
    resolve_repo_facts,
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
# ADR-035 chunk 12: bedrock_client is now EMBEDDINGS-ONLY (Titan via
# invoke_model). LLM completions go through shared.model_client.invoke
# (imported above). The 300s read timeout on bedrock_config is no
# longer load-bearing -- it was sized for Claude's 2-3 minute summary
# responses, which now route through shared.model_client's own client.
# Titan embeddings return in <1s. Kept for now; can tighten in a
# follow-up if cold-start matters.
bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
ssm_client = boto3.client("ssm")
s3vectors_client = boto3.client("s3vectors")

# Chunk 05: SQS client for merge-message requeue-with-delay. Queue URL
# comes from CODE_MAPS_QUEUE_URL env var (set by terraform chunk 06).
# Fallback: if missing, merge handler logs a warning and processes as
# if shards are complete (worst case: merge fires early and produces
# a partial merged summary rather than blocking sha-truth advance).
sqs_client = boto3.client("sqs")
CODE_MAPS_QUEUE_URL = os.environ.get("CODE_MAPS_QUEUE_URL", "")

# Chunk 05: how many merge-requeue-with-delay attempts before we give
# up and mark the merge failed. Historical default was 10 x 60s = ~10 min
# grace for the last shard to land. fable-scale chunk 08 makes both
# tunable via env — enterprise repo counts want a longer runway (30 x 60s
# = 30 min default) since some shards genuinely take longer than 10 min.
_MAX_MERGE_ATTEMPTS = int(os.environ.get("BATCH_SUMMARY_MERGE_ATTEMPTS", "30"))
_MERGE_REQUEUE_DELAY_SECONDS = int(
    os.environ.get("BATCH_SUMMARY_MERGE_DELAY_SECONDS", "60")
)


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    keeps module load order safe."""
    from shared.github_base import github_api_base
    return github_api_base()


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
KB_BUCKET = os.environ.get("KB_BUCKET")

GITHUB_API_URL = f"{_github_api()}"


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
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            default_branch = data.get("default_branch", "main")
            _default_branch_cache[repo] = default_branch
            logger.info(f"Default branch for {repo}: {default_branch}")
            return default_branch
    except URLError as e:
        logger.warning(f"Failed to get default branch for {repo}, using 'main': {e}")
        return "main"


def fetch_file_content(
    repo: str,
    file_path: str,
    *,
    provider: str = "github",
    workspace_id: str = None,
    slug: str = None,
) -> str:
    """Fetch raw file content for the code-map / symbol-graph prompt.

    Dispatches on ``provider``:
    - "github" (default for back-compat): live raw.githubusercontent.com
      with the platform's PAT.
    - "azure_devops": reads the S3 mirror that azure-devops-sync
      wrote during ingestion. Caller must pass workspace_id + slug
      (org/project/repo) for the key shape
      ``workspaces/{ws}/azure_devops/{slug}/{path}``.

    GitLab (ADR-050) follows the same S3-mirror pattern as azure_devops:
    ``workspaces/{ws}/gitlab/{slug}/{path}``.

    See lambda/shared/source_fetch.py for the dispatch contract.
    """
    from shared.source_fetch import fetch_repo_file, SourceFetchError

    try:
        if provider in ("azure_devops", "gitlab"):
            # both write an S3 source mirror during sync; source_fetch reads
            # workspaces/{ws}/{provider}/{slug}/{path}.
            return fetch_repo_file(
                provider,
                file_path,
                workspace_id=workspace_id,
                slug=slug,
                kb_bucket=KB_BUCKET,
            )
        # github path -- keep the existing get_default_branch + PAT
        # behavior. fetch_repo_file's github branch only needs the
        # already-resolved default branch + token.
        default_branch = get_default_branch(repo)
        return fetch_repo_file(
            "github",
            file_path,
            repo_project=repo,
            default_branch=default_branch,
            github_token=GITHUB_TOKEN,
        )
    except SourceFetchError as e:
        # Map to the previous logger + raise behavior so the rest of
        # the pipeline (per-file try/except + retry) is unchanged.
        logger.error(f"Failed to fetch {file_path}: {e}")
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

# MAX_PROMPT_TOKENS + estimate_tokens now live in shared.code_map_sizing
# (imported at the top of the module) so process-repo-summary's upstream
# splitter uses the same "does this fit in one call" heuristic.


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


# Graph-first grounding: cap how many verified symbols/refs we inline so
# the skeleton anchors the summary without bloating the prompt (ADR-027
# follow-up — the symbol graph is parser-extracted ground truth for names,
# relationships, and file:line).
_GROUNDING_MAX_DEFINES = 120
_GROUNDING_MAX_REFS = 60
_GROUNDING_REF_LABELS = {
    "calls": "Calls (outbound)",
    "imports": "Imports",
    "implements": "Implements",
}


def _render_signature(sig) -> str:
    """Render a Signature as a parenthesized parameter list with an
    optional ``-> return_type`` tail. Returns "" for an empty signature
    so callers can skip the suffix entirely."""
    if sig is None:
        return ""
    parts = []
    for p in getattr(sig, "params", ()) or ():
        name = getattr(p, "name", "")
        type_str = getattr(p, "type", None)
        parts.append(f"{name}: {type_str}" if type_str else name)
    returns = getattr(sig, "returns", None)
    suffix = f" -> {returns}" if returns else ""
    if not parts and not returns:
        return ""
    return f"({', '.join(parts)}){suffix}"


def _render_graph_grounding(facts: "list[Fact]") -> str:
    """Render parser-extracted facts as a verified-symbol skeleton for the
    code-map prompt (graph-first generation).

    The symbol graph is ground truth for symbol names, relationships, and
    file:line. Handing it to the summarizer with a "use only these"
    constraint stops invented symbols, hallucinated cross-references, and
    wrong call-site attribution. Bounded so it anchors without bloating
    the prompt. Returns "" when no facts (e.g. fact_sink not provided),
    leaving the prompt unchanged for legacy/test paths.

    When a ``defines`` fact carries a ``signature`` (chunk 01 of
    plan-signature-extraction), the rendered line includes the
    parenthesized parameter list + return type. The summarizer is told
    to use ONLY those verified signatures, closing the
    wrong-param-order / wrong-type class of hallucination the same way
    the symbol-name grounding closes invented-symbol hallucinations.
    """
    if not facts:
        return ""
    defined_ids = {f.target_symbol_id for f in facts if f.kind == "defines"}
    defines: List = []
    seen = set()
    outbound: Dict[str, set] = {}
    any_signed = False
    for f in facts:
        if f.kind == "defines":
            key = (f.target_symbol_id, f.file_path, f.line)
            if key not in seen:
                seen.add(key)
                sig = getattr(f, "signature", None)
                if sig is not None:
                    any_signed = True
                defines.append((f.target_symbol_id, f.file_path, f.line, sig))
        elif f.kind in _GROUNDING_REF_LABELS:
            # Outbound references only — targets not defined in this group.
            if f.target_symbol_id not in defined_ids:
                outbound.setdefault(f.kind, set()).add(f.target_symbol_id)
    if not defines and not outbound:
        return ""
    header_extra = (
        " For any function/method listed with a parenthesized signature, "
        "use that EXACT signature — do not reorder parameters, infer "
        "missing types, or invent a return type."
        if any_signed
        else ""
    )
    lines = [
        "## VERIFIED SYMBOLS (parser-extracted ground truth — use these EXACTLY)",
        "",
        "These were extracted from the actual source by the parser. Use these "
        "exact symbol names and file:line locations. Do NOT introduce a "
        "function, class, call, import, or cross-reference that is not listed "
        "here; if tempted to, omit it. This is the authoritative structure — "
        "the source files below are for narrative and behavior only."
        + header_extra,
    ]
    if defines:
        lines.append("")
        lines.append("Defined in this group:")
        for sym, fp, ln, sig in sorted(defines, key=lambda x: x[:3])[
            :_GROUNDING_MAX_DEFINES
        ]:
            sig_str = _render_signature(sig)
            lines.append(
                f"- {sym}{sig_str}  ({fp}:{ln})" if sig_str else f"- {sym}  ({fp}:{ln})"
            )
    for kind in ("calls", "imports", "implements"):
        targets = sorted(outbound.get(kind, ()))[:_GROUNDING_MAX_REFS]
        if targets:
            lines.append("")
            lines.append(f"{_GROUNDING_REF_LABELS[kind]}:")
            for t in targets:
                lines.append(f"- {t}")
    return "\n".join(lines)


def _build_batch_prompt(
    batch_type: str,
    repo: str,
    group_name: str,
    files_section: str,
    graph_grounding: str = "",
) -> str:
    """Build prompt based on batch type with the given files section."""
    prompt_templates = {
        "infrastructure": f"Summarize the infrastructure for {repo} based on these Terraform files. Describe what resources are created, how they're organized, and the overall architecture.\n\n{files_section}",
        "handler-group": f"""Write a CONCISE implementation overview of the {group_name} Lambda handler for {repo}. Target ~3000 words. A reader uses this summary as an INDEX to navigate the source -- they will read source for exact values. Do not turn this into a documentation rewrite.

Discipline:
- Group functions by what they DO. Don't document each function in its own subsection unless it is genuinely unique. Helper functions, similar CRUD functions, and lazy-init client factories should be summarized as one paragraph naming the group, not enumerated.
- State each pattern ONCE. If five functions all read SSM the same way, write "The handler reads SSM parameters via a cached client" once and list which functions use it. Don't repeat the pattern in each function's section.
- Skip cross-references unless they're load-bearing for understanding (e.g. an unobvious dependency on another Lambda). Don't list every shared utility import.
- If a function is trivial (one-line wrapper, getter, formatter), do not document it. Mention by name only.

Cover these themes, blended into prose -- not separate per-function rubric:

1. Module role + trigger model. What invokes the handler (SQS, EventBridge, API Gateway, DynamoDB Streams). Top-level dispatch logic.
2. The principal user-facing functions (typically 2-5 per handler). For each, briefly: what it does, what calls it, what it returns on success vs failure (outcome category, not exact codes), and the one or two pitfalls that matter.
3. Datastore / external-system interactions, by KIND not by exact identifier. Name the FIELDS schema, not the table-name strings. Mention "writes to a DynamoDB table keyed by a customer-scoped prefix" rather than listing exact key strings.
4. Notable design decisions or non-obvious behaviors that a debugger would benefit from knowing (e.g. "blacklisted users receive a 200 silently to avoid enumeration").

Skip:
- Exhaustive parameter tables. Name the inputs in prose.
- Function-by-function signature blocks. The symbol graph already carries signatures.
- Cross-reference link dumps. The reader has source access.
- Pitfall examples for trivial mistakes. Only flag pitfalls that take >5min to debug.

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

    base_prompt = prompt_templates.get(
        batch_type,
        f"Summarize these {group_name} files for {repo}.\n\n{files_section}"
    )
    # Verified symbols first (graph-first ground truth), then the task +
    # source, then the literal-omission discipline (ADR-027) last as the
    # override. Grounding is empty when no facts were extracted (legacy/
    # test paths) — prompt is then unchanged.
    grounding_section = f"{graph_grounding}\n\n" if graph_grounding else ""
    return f"{grounding_section}{base_prompt}\n\n{CODE_MAP_LITERAL_DISCIPLINE}"


def _call_bedrock_and_audit(prompt: str, group_name: str) -> str:
    """Call the configured model and log audit entry. Returns summary text.

    Routes via shared.model_client.invoke (ADR-035) -- Converse for
    anthropic/meta/nova/gpt-oss; Mantle Responses for openai.gpt-5*/codex.
    Low temperature for code-map generation (ADR-027 literal-omission
    discipline pairs with this; sampling noise was a root cause of the
    value/identifier hallucination that froze wrong literals into the
    artifact).
    """
    def call_model():
        # ADR-016 v1.2 -- code-maps workload attribution.
        resp = invoke_model(
            workload="code-maps",
            tier="advanced",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            max_tokens=8192,
            temperature=0.1,
        )
        if not resp.text:
            raise ValueError(
                f"No text in {ADVANCED_MODEL_ID} response "
                f"(stop_reason={resp.stop_reason})"
            )
        return resp.text, resp.usage

    bedrock_start = time.time()
    summary, usage = retry_bedrock_call(call_model)
    bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)
    logger.info(f"Generated batch summary for {group_name} ({len(summary)} chars)")

    log_audit_entry(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        lambda_name="process-batch-summary",
        model_id=ADVANCED_MODEL_ID,
        input_text=prompt,
        output_text=summary,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
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
            extractor runs on the raw fetched bytes BEFORE chunking so
            symbol resolution sees complete files. When ``None`` the
            extractor pass is skipped — keeps the existing call sites
            (tests, legacy paths) unchanged.
        sg_filter: Per-repo symbol-graph extraction filter, applied
            per-file inside ``_iter_batch_facts``. Default is the
            permissive module-level filter, so legacy call sites keep
            their current behaviour.

    Returns:
        Generated summary text
    """
    repo = batch["repo"]
    repo_project = batch["repo_project"]
    batch_type = batch["batch_type"]
    group_name = batch["group_name"]
    file_paths = batch["file_paths"]

    # chunk-08a provider-aware fetch: process-repo-summary threads
    # `provider` + `workspace_id` + `slug` through to the code-units
    # message. For azure_devops these route fetch_file_content to the
    # S3 mirror written by azure-devops-sync (chunk 05a). For github
    # (legacy / absent provider) the existing raw.githubusercontent.com
    # path runs unchanged.
    fetch_provider = batch.get("provider", "github")
    fetch_workspace_id = batch.get("workspace_id")
    fetch_slug = batch.get("slug")

    # Fetch all file contents
    file_contents = []
    # This batch's extracted facts, accumulated for graph-first grounding
    # of the map prompt (alongside the caller's fact_sink, which may span
    # batches or be None).
    batch_facts: List = []

    for file_path in file_paths:
        try:
            content = fetch_file_content(
                repo_project,
                file_path,
                provider=fetch_provider,
                workspace_id=fetch_workspace_id,
                slug=fetch_slug,
            )

            # Symbol-graph extractor pass. Run on the FULL raw content,
            # not the chunked / truncated copy — truncation would drop
            # symbol definitions and produce spurious "no callers"
            # results. Best-effort, per-file try/except: an extractor
            # crash never blocks the summary.
            if fact_sink is not None:
                language = _language_for_path(file_path)
                if language is not None:
                    file_facts = list(
                        _iter_batch_facts(
                            file_path, language, content, sg_filter
                        )
                    )
                    batch_facts.extend(file_facts)
                    fact_sink.extend(file_facts)

            # Chunk large files instead of skipping them
            chunks = chunk_large_content(content, file_path)
            file_contents.extend(chunks)

        except Exception as e:
            logger.error(f"Error fetching file {file_path}: {e}")
            # Continue with other files

    if not file_contents:
        return "No files available for analysis"

    # Graph-first grounding: the verified skeleton extracted above anchors
    # the summary's structure (names, edges, file:line). Empty when no
    # facts were extracted (fact_sink None) — prompt then unchanged.
    grounding = _render_graph_grounding(batch_facts)

    # Build full prompt and check token estimate
    files_section = "\n".join([_format_file_content(fc) for fc in file_contents])
    prompt = _build_batch_prompt(
        batch_type, repo, group_name, files_section, grounding
    )
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

    # Chunk 04: sub-batching is now a defense-in-depth safety net.
    # Post-chunk-03 shard messages are pre-sized to fit in one call, so
    # this branch triggering on a shard indicates the upstream
    # estimator drifted (e.g., an unusually dense-tokenizing file).
    # Log loudly so the drift is observable in CloudWatch metrics.
    shard_count = batch.get("shard_count", 1)
    if shard_count > 1:
        logger.warning(
            f"[splitter-drift] shard message triggered sub-batching: "
            f"{group_name} ({estimated_tokens} estimated tokens > "
            f"{MAX_PROMPT_TOKENS} ceiling). Splitter under-sized this "
            f"shard; investigate token-estimator accuracy."
        )
    logger.info(
        f"Batch too large ({estimated_tokens} estimated tokens), "
        f"splitting into {len(sub_batches)} sub-batches"
    )

    # Summarize each sub-batch
    sub_summaries = []
    for i, sub_fc_list in enumerate(sub_batches, 1):
        logger.info(f"Processing sub-batch {i}/{len(sub_batches)} ({len(sub_fc_list)} file chunks)")
        sub_files_section = "\n".join([_format_file_content(fc) for fc in sub_fc_list])
        sub_prompt = _build_batch_prompt(
            batch_type, repo, group_name, sub_files_section, grounding
        )
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


# Titan Embeddings v2 hard input cap. Bedrock rejects InvokeModel with
# ValidationException "Malformed input request: expected maxLength: 50000"
# above this. The prompt update lands summaries at ~3000 words / ~20k
# chars under normal behavior; this constant is a safety net so a
# verbose-day summary still gets a vector instead of silently failing
# the whole code-map. Live-burned 2026-06-28: 27 of 31 handler-group
# summaries on app.retrieveit.ai failed embedding because the legacy
# prompt produced 58k+ char summaries.
_EMBEDDING_INPUT_CHAR_LIMIT = 45000


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding using Bedrock Titan Embeddings v2.

    Args:
        text: Text to embed

    Returns:
        1024-dimensional embedding vector
    """
    if len(text) > _EMBEDDING_INPUT_CHAR_LIMIT:
        # Truncate at the safety net. The full text still lives in the
        # vector metadata + S3 (process-batch-summary writes both); only
        # the embedding's semantic anchor is truncated. The first ~45k
        # chars of a code-map summary carry the structural overview;
        # the dropped tail is typically the trailing per-function detail
        # that would muddy the embedding's semantics anyway.
        logger.warning(
            "Embedding input %d chars exceeds %d limit; truncating. "
            "Code-map prompt should produce shorter summaries -- a "
            "consistent miss here means the prompt update lost an "
            "instance.",
            len(text), _EMBEDDING_INPUT_CHAR_LIMIT,
        )
        text = text[:_EMBEDDING_INPUT_CHAR_LIMIT]

    def call_titan():
        payload = {
            "inputText": text,
            "dimensions": 1024,
            "normalize": True,
        }

        # ADR-016 v1.2 -- chat/titan-embed profile ARN when wired, else
        # bare foundation model (untagged fallback).
        embed_model_id = (
            resolve_profile_arn("chat", "titan-embed") or EMBEDDING_MODEL_ID
        )
        response = bedrock_client.invoke_model(
            modelId=embed_model_id,
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
        # chunk-08a: provider is threaded through from process-repo-
        # summary's send_code_units_to_sqs. Default "github" preserves
        # all existing vector keys + metadata.
        provider = batch.get("provider", "github")

        # Construct vector key on the BARE repo name (last path segment),
        # uniform across providers (ADR-052). GitHub/ADO already pass a
        # bare batch['repo'] (no owner/org/project), so rsplit is a no-op
        # for them; GitLab passes the full slug ("group/subgroup/project")
        # and is normalised here to the bare project name so its
        # code-map/summary vectors match the {ws}#{source}#{bare}#... shape
        # that the delete-worker's code-map sweep purges on repo removal.
        # Without this, GitLab code-map vectors keyed on the slug leaked
        # into the RAG after a repo was deleted (delete-bug: GitLab).
        # Workspace repos: {workspace_id}#{provider}#{repo_bare}#{doc_type}#{group_name}
        # Legacy repos: {repo_bare}#{doc_type}#{group_name}
        repo_bare = batch["repo"].rsplit("/", 1)[-1]
        if workspace_id:
            vector_key = f"{workspace_id}#{provider}#{repo_bare}#{doc_type}#{batch['group_name']}"
        else:
            vector_key = f"{repo_bare}#{doc_type}#{batch['group_name']}"

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
            metadata["source"] = provider
        if batch.get("commit_sha"):
            # Generation marker: shard keys are deterministic, so a shard
            # vector from a PREVIOUS run is indistinguishable from this
            # run's by key alone. The merge fan-in compares this against
            # the merge message's commit_sha to avoid merging stale
            # shards (the code-maps queue is not FIFO, so the merge can
            # race ahead of its own shards).
            metadata["commit_sha"] = batch["commit_sha"]

        # fable-scale chunk 07: route through shared.dual_write so this
        # write replicates to the passive region.
        from shared import dual_write
        dual_write.s3vectors_put_vectors(
            bucket=VECTOR_BUCKET,
            index=VECTOR_INDEX,
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
    """Hand the Fact stream to the symbol-graph writer.

    When ``workspace_id`` / ``repo`` / ``commit_sha`` are all present the
    Facts are persisted via the delete-then-write path in
    ``shared.symbol_graph_writer``. The pointer state machine in
    ``shared.symbol_graph_pointer`` owns the actual ``graph_sha``
    advance, so the writer is invoked with ``skip_pointer_swap=True``
    to keep the legacy ``current_commit_sha`` write from clobbering
    the new ``graph_sha`` / ``graph_pending_sha`` attributes on the
    pointer row.

    Args:
        facts: Iterable of extractor Facts.
        workspace_id / repo / commit_sha: Storage context. When any are
            missing the call is a no-op log (legacy producer path).
        previous_graph_sha: Caller-supplied previous SHA from the
            pointer module. Bypasses the writer's legacy pointer read
            so it doesn't reach for a code-maps row that no longer
            carries the source-of-truth pointer.

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


def _list_workspace_repos(workspace_id: str, *, exclude_bare: str = "") -> "list[str]":
    """Bare repo names connected to ``workspace_id`` (ADR-056), excluding
    ``exclude_bare``.

    The workspaces table stores one row per connected repo at
    ``PK=WS#{workspace_id}``, ``SK=GITHUB_REPO#{owner}/{repo}`` (see
    feedback_ddb_repo_sk_convention -- it is ``GITHUB_REPO#``, NOT ``REPO#``).
    The symbol-graph table keys on the BARE repo name (reference_app_vs_repo_naming),
    so strip the owner prefix. Best-effort: returns [] on any error.
    """
    table = _get_workspaces_table()
    if table is None:
        return []
    from boto3.dynamodb.conditions import Key

    bare_names: "list[str]" = []
    query_kwargs: "dict" = {
        "KeyConditionExpression": Key("PK").eq(f"WS#{workspace_id}")
        & Key("SK").begins_with("GITHUB_REPO#"),
        "ProjectionExpression": "SK",
    }
    try:
        while True:
            resp = table.query(**query_kwargs)
            for item in resp.get("Items", []):
                sk = str(item.get("SK", ""))
                name = sk[len("GITHUB_REPO#"):] if sk.startswith("GITHUB_REPO#") else ""
                bare = name.rsplit("/", 1)[-1]
                if bare and bare != exclude_bare and bare not in bare_names:
                    bare_names.append(bare)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "ADR-056 sibling-repo enumeration failed for %s: %s", workspace_id, exc
        )
        return []
    return bare_names


def _resolve_definition_scope_pairs(
    workspace_id: str, repo: str
) -> "list[tuple[str, str]]":
    """``(owning_workspace, bare_repo)`` pairs for every repo in the resolution
    scope of ``(workspace_id, repo)`` (ADR-056 §1).

    The scope is this workspace plus every ``is_global`` workspace plus every
    workspace that granted this one access (``search_allowed_workspaces``) --
    the same scope the BREAKING_CHANGES consumer query uses (shared
    ``pr_check_scope`` primitives), so the graph and the check agree on
    visibility. Each sibling is paired with its OWN owning workspace because the
    symbol-graph PK is ``{workspace_id}#{repo}``: a global/granted producer in
    workspace B is scanned as ``B#repo``, not ``{this_workspace}#repo``.

    The ``(workspace_id, repo)`` self pair is excluded (the repo's own defines
    are already in the primary index). A different repo that merely shares the
    bare name in another in-scope workspace is a legitimately different repo and
    is kept. Best-effort: on any scope-resolution failure, falls back to the
    own-workspace siblings only (the pre-ADR-056-scope behavior).
    """
    scope_ws = {workspace_id}
    ws_table = _get_workspaces_table()
    if ws_table is not None:
        try:
            from shared.pr_check_scope import (  # type: ignore
                list_global_workspace_ids,
                list_workspaces_granting_access,
            )

            scope_ws.update(list_global_workspace_ids(table=ws_table))
            scope_ws.update(
                list_workspaces_granting_access([workspace_id], table=ws_table)
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ADR-056 scope resolution failed for %s/%s; own-workspace "
                "only: %s",
                workspace_id, repo, exc,
            )
            scope_ws = {workspace_id}

    pairs: "list[tuple[str, str]]" = []
    for ws in sorted(scope_ws):
        exclude = repo if ws == workspace_id else ""
        for sibling in _list_workspace_repos(ws, exclude_bare=exclude):
            pairs.append((ws, sibling))
    return pairs


def _load_workspace_sibling_defines(workspace_id: str, repo: str) -> "list[Fact]":
    """Load the ``defines`` of every OTHER repo in the resolution SCOPE (ADR-056)
    as lookup-only facts for the cross-repo definitions index.

    The scope spans this workspace + ``is_global`` workspaces + workspaces that
    granted this one access (see ``_resolve_definition_scope_pairs``). Each
    sibling is scanned under its own owning-workspace partition.

    Best-effort per sibling: a load failure drops that one sibling, never the
    whole resolution pass. Only ``defines`` are loaded -- they widen the index
    so a consumer's cross-repo reference can bind to a producer repo's symbol;
    nothing from a sibling is ever rewritten or emitted (resolve_repo_facts
    treats ``extra_definitions`` as lookup-only).

    Cost note: scans each in-scope sibling partition and filters to defines. For
    large scopes this is the dominant cost of the post-pass; it is bounded by the
    scope's repo count x definition count and uses the projected scan.
    """
    from shared.symbol_graph_writer import scan_repo_facts

    pairs = _resolve_definition_scope_pairs(workspace_id, repo)
    defines: "list[Fact]" = []
    for owning_ws, sibling in pairs:
        try:
            for row in scan_repo_facts(owning_ws, sibling):
                if row.get("kind") != "defines":
                    continue
                defines.append(
                    Fact(
                        kind="defines",
                        source_symbol_id=str(row.get("source_symbol_id", "")),
                        target_symbol_id=str(row.get("target_symbol_id", "")),
                        file_path=str(row.get("file_path", "")),
                        # line is irrelevant for a definition in the index; guard
                        # the >=1 invariant rather than drop a sibling over it.
                        line=max(1, int(row.get("line", 1) or 1)),
                        language=str(row.get("language", "")),
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ADR-056 sibling defines load failed for %s/%s: %s",
                owning_ws, sibling, exc,
            )
            continue
    if defines:
        logger.info(
            "ADR-056 cross-repo: loaded %d sibling defines from %d repo(s) "
            "across %d workspace(s) in scope for %s/%s",
            len(defines), len(pairs), len({ws for ws, _ in pairs}),
            workspace_id, repo,
        )
    return defines


def _resolve_repo_symbol_graph(
    workspace_id: str, repo: str, commit_sha: str
) -> None:
    """ADR-055 whole-repo cross-module resolution post-pass.

    Runs once per repo at the last-close of a FULL regen (the caller gates
    this on the ``full_regen`` message flag). Reads every stored fact for the
    (workspace, repo) partition, rebinds each cross-module
    ``calls``/``imports``/``extends`` target to its definition id via the
    registered per-language resolver, and replaces only the edges whose
    target actually changed.

    Why here and not per-batch: resolution needs the repo-wide definitions
    index, but the regen writes facts one file-group per invocation, so no
    single batch sees every ``defines``. The last-close batch is the first
    moment the full generation exists in the graph. Keyed on the BARE repo
    name (the symbol-graph table PK namespace), matching ``_emit_facts``.

    Best-effort: any failure leaves the freshly-written (unresolved but
    valid) generation intact -- resolution simply does not apply this run,
    and the next full regen retries. Never raises.
    """
    try:
        from shared.symbol_graph_writer import (  # type: ignore
            rewrite_resolved_targets,
            scan_repo_facts,
        )

        rows = scan_repo_facts(workspace_id, repo)
        if not rows:
            return

        facts: "list[Fact]" = []
        sks: "list[str]" = []
        for row in rows:
            sk = row.get("SK")
            kind = row.get("kind")
            if not sk or not kind:
                continue
            facts.append(
                Fact(
                    kind=str(kind),
                    source_symbol_id=str(row.get("source_symbol_id", "")),
                    target_symbol_id=str(row.get("target_symbol_id", "")),
                    file_path=str(row.get("file_path", "")),
                    line=int(row.get("line", 0) or 0),
                    language=str(row.get("language", "")),
                )
            )
            sks.append(str(sk))

        # ADR-056: resolve against the workspace-wide definitions index so a
        # cross-repo reference (a consumer repo calling a producer repo's symbol)
        # binds to the producer's definition. Best-effort: on any sibling-load
        # failure, fall back to within-repo (ADR-055) resolution only.
        try:
            sibling_defines = _load_workspace_sibling_defines(workspace_id, repo)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ADR-056 sibling-defines load failed; within-repo only: %s", exc
            )
            sibling_defines = []

        # resolve_repo_facts returns a list parallel to its input, so each
        # resolved fact lines up with the SK of the row it came from.
        resolved = resolve_repo_facts(facts, extra_definitions=sibling_defines)
        rewrites = [
            (sks[i], resolved[i])
            for i in range(len(facts))
            if resolved[i].target_symbol_id != facts[i].target_symbol_id
        ]
        if not rewrites:
            logger.info(
                "symbol-graph resolution: no cross-module targets rebound "
                "for %s/%s@%s (%d facts scanned)",
                workspace_id,
                repo,
                commit_sha,
                len(facts),
            )
            return

        result = rewrite_resolved_targets(
            workspace_id, repo, commit_sha, rewrites
        )
        logger.info(
            "symbol-graph resolution: rebound %d targets (deleted %d stale "
            "rows) for %s/%s@%s (%d facts scanned)",
            result.rewritten,
            result.deleted,
            workspace_id,
            repo,
            commit_sha,
            len(facts),
        )
    except Exception as exc:  # pragma: no cover - defensive, never break regen
        logger.warning(
            "symbol-graph resolution post-pass failed for %s/%s@%s: %s "
            "(generation left unresolved; next full regen retries)",
            workspace_id,
            repo,
            commit_sha,
            exc,
        )


def _enqueue_xrepo_reresolve(
    workspace_id: str, producer_repo: str, commit_sha: str
) -> None:
    """ADR-056 v2: enqueue a cross-repo re-resolution for a producer repo
    that just finished a full regen.

    Fires from the producer's last-close branch, AFTER the producer's own
    resolution post-pass. Decouples the (potentially fanning-out) consumer
    re-resolution from the producer's critical ingest path -- the message is
    consumed by this same Lambda via the code-maps queue and handled by
    ``handle_xrepo_reresolve_message``. Best-effort: a send failure only
    means consumers pick up the producer's change on their own next regen
    (the v1 consumer-pull contract), never a broken producer ingest.
    """
    if not CODE_MAPS_QUEUE_URL:
        logger.warning(
            "[xrepo] CODE_MAPS_QUEUE_URL not configured; cannot enqueue "
            "re-resolution for producer %s/%s",
            workspace_id,
            producer_repo,
        )
        return
    message = {
        "message_type": "xrepo_reresolve",
        "workspace_id": workspace_id,
        "repo": producer_repo,
        "commit_sha": commit_sha,
    }
    try:
        sqs_client.send_message(
            QueueUrl=CODE_MAPS_QUEUE_URL,
            MessageBody=json.dumps(message),
        )
        logger.info(
            "ADR-056 v2: enqueued cross-repo re-resolution for producer "
            "%s/%s@%s",
            workspace_id,
            producer_repo,
            commit_sha,
        )
    except ClientError as exc:
        logger.warning(
            "[xrepo] failed to enqueue re-resolution for producer %s/%s: %s",
            workspace_id,
            producer_repo,
            exc,
        )


def _find_importer_repos(workspace_id: str, producer_repo: str) -> "list[str]":
    """Bare names of the repos that import ``producer_repo``'s definitions
    (ADR-056 v2, importer-narrowed).

    Scans the producer's ``defines`` to get its definition ids, then queries
    the target GSI (``symbol_graph_reader.importers``) for each -- the same
    index that already powers cross-repo ``find_importers``. Returns the
    distinct set of consumer repos, excluding the producer itself. Best-effort:
    a failure on any single definition drops that lookup, never the whole set.
    """
    from shared.symbol_graph_reader import importers  # type: ignore
    from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

    try:
        define_ids = [
            str(row.get("target_symbol_id", ""))
            for row in scan_repo_facts(workspace_id, producer_repo)
            if row.get("kind") == "defines" and row.get("target_symbol_id")
        ]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[xrepo] could not scan producer defines for %s/%s: %s",
            workspace_id,
            producer_repo,
            exc,
        )
        return []

    repos: "set[str]" = set()
    for definition_id in define_ids:
        try:
            result = importers(
                workspace_id,
                definition_id,
                code_maps_table_name=CODE_MAPS_TABLE,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[xrepo] importers() failed for %s: %s", definition_id, exc
            )
            continue
        for site in result.sites:
            bare = str(site.repo or "").rsplit("/", 1)[-1]
            if bare and bare != producer_repo:
                repos.add(bare)
    return sorted(repos)


def _current_graph_sha(workspace_id: str, repo: str) -> "str | None":
    """The committed graph SHA for a (workspace, repo), or None.

    Used to stamp a consumer's re-resolved rows with the consumer's OWN
    provenance SHA (ADR-056 v2) rather than the producer's commit, since the
    consumer source did not change -- only its cross-repo bindings refresh.
    """
    try:
        from shared.symbol_graph_pointer import read_pointer  # type: ignore

        state = read_pointer(
            workspace_id, repo, code_maps_table_name=CODE_MAPS_TABLE
        )
        return state.graph_sha
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[xrepo] could not read graph sha for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        return None


def handle_xrepo_reresolve_message(batch: Dict[str, Any]) -> None:
    """ADR-056 v2 producer-push: re-resolve the consumers of a changed producer.

    Finds the repos that import the producer (importer-narrowed via the target
    GSI) and re-runs the whole-repo resolution post-pass for each against the
    now-current workspace-wide definitions index. This reuses the v1 post-pass
    (``_resolve_repo_symbol_graph``) verbatim, which is exactly right:

    - A consumer ingested BEFORE its producer has raw cross-repo calls
      (``app.main::add``); with the producer's defines now in the index they
      bind to ``shared_lib.math_utils::add`` -- this closes the ADR-056 §5
      ordering gap, the primary value of v2.
    - An already-bound edge re-resolves to the same id (``bound == target`` ->
      no rewrite): idempotent, so a stable workspace does no writes.
    - An edge bound to a producer symbol the producer has since REMOVED does
      not resolve, so it is left in place (not reverted to raw). That dangling
      edge IS the breaking signal on the producer's own PR, and it self-heals
      on the consumer's next regen. v2 deliberately does not un-bind.

    Never marks a code-map ledger (this message is not part of any regen's unit
    count) and never raises -- a failure just leaves consumers on the v1
    consumer-pull contract.
    """
    workspace_id = batch.get("workspace_id") or ""
    producer_repo = batch.get("repo") or ""
    producer_sha = batch.get("commit_sha") or ""
    if not (workspace_id and producer_repo):
        logger.warning(
            "[xrepo] re-resolution message missing workspace_id/repo; dropping"
        )
        return

    consumers = _find_importer_repos(workspace_id, producer_repo)
    if not consumers:
        logger.info(
            "ADR-056 v2: producer %s/%s has no importer repos; nothing to "
            "re-resolve",
            workspace_id,
            producer_repo,
        )
        return

    logger.info(
        "ADR-056 v2: re-resolving %d importer repo(s) of producer %s/%s: %s",
        len(consumers),
        workspace_id,
        producer_repo,
        ", ".join(consumers),
    )
    for consumer in consumers:
        # Stamp the consumer's own graph SHA; fall back to the producer's only
        # if the pointer read fails (rows stay addressable either way -- the
        # symbol-graph PK carries no SHA; the SHA is provenance).
        consumer_sha = _current_graph_sha(workspace_id, consumer) or producer_sha
        if not consumer_sha:
            logger.warning(
                "[xrepo] no commit sha for consumer %s/%s; skipping",
                workspace_id,
                consumer,
            )
            continue
        _resolve_repo_symbol_graph(workspace_id, consumer, consumer_sha)


def _iter_batch_facts(
    file_path: str,
    language: str,
    content: str,
    config: SymbolGraphFilter = DEFAULT_SYMBOL_GRAPH_FILTER,
) -> Iterator[Fact]:
    """Yield Facts for a single file that already has its content in hand.

    ``config`` is the per-repo extraction filter; when omitted the
    permissive default is used so legacy call sites keep their current
    behavior.
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
    ``.outcomeops.yaml``.

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

    # Chunk 02 schema: messages carry message_type + shard fields.
    # Old-shape messages (pre-chunk-02 producer or the 10 stuck DLQ
    # entries from before this migration) default to single-shard --
    # they still process end-to-end via the existing summarize path.
    message_type = batch.get("message_type", "shard")
    if message_type == "merge":
        # Chunk 05: fan-in merge. Reads N shard summaries by key,
        # generates one unified summary via Bedrock, stores at parent
        # vector key. Requeue-with-delay when shards not yet complete.
        # Marks ledger on the final iteration (success OR max-attempts
        # give-up); the requeue path does NOT mark so the ledger tracks
        # the merge as still pending.
        handle_merge_message(batch)
        return

    if message_type == "xrepo_reresolve":
        # ADR-056 v2 producer-push re-resolution. A producer repo just
        # completed a full regen; re-resolve the repos that IMPORT it so
        # their cross-repo bindings pick up the producer's defines without
        # re-ingesting each consumer. Importer-narrowed via the target GSI;
        # best-effort and self-contained (never marks a code-map ledger).
        handle_xrepo_reresolve_message(batch)
        return

    shard_index = batch.get("shard_index", 0)
    shard_count = batch.get("shard_count", 1)
    parent_storage_key = batch.get("parent_storage_key") or batch["storage_key"]

    logger.info(
        f"Processing {batch['batch_type']} batch: {batch['group_name']} "
        f"({len(batch['file_paths'])} files) "
        f"shard={shard_index + 1}/{shard_count} parent={parent_storage_key}"
    )

    # Symbol-graph regen context. All three of workspace_id, repo, and
    # commit_sha must be present for the atomic-swap path to engage;
    # legacy producers that omit any of them fall through to the
    # existing summary-only flow.
    sg_workspace = batch.get("workspace_id") or ""
    sg_repo = batch.get("repo") or ""
    # repo_project is owner/repo (the canonical SK shape used by the
    # workspaces row and the SHA-truth ledger PK); sg_repo is the
    # short name used by the symbol-graph table key. Both come from
    # the same SQS body so we extract them together.
    repo_project = batch.get("repo_project") or sg_repo
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
    # Per-repo .outcomeops.yaml ``ingestion.symbol_graph`` knobs only
    # matter on the extraction path, so the filter is only loaded when
    # we've already claimed the swap. On the legacy / sg-disabled path
    # the filter is irrelevant.
    try:
        if sg_claimed:
            # GitHub API calls need the full ``owner/repo`` slug;
            # ``sg_repo`` is the bare short name used only as the
            # symbol-graph fact-store namespace key. Pre-fix these
            # calls received ``sg_repo`` (short), which 404'd against
            # ``/repos/{name}`` and fell back to ``"main"``.
            # Coincidentally correct for main-defaulted repos, silently
            # wrong for master/develop/trunk. Worse: the same short
            # name was handed to ``_load_symbol_graph_filter`` whose
            # ``load_repo_config_from_github`` call also 404'd,
            # silently swallowing the per-repo
            # ``.outcomeops.yaml::ingestion.symbol_graph`` config.
            # ``repo_project`` (owner/repo) resolves both correctly.
            # Fact-store keys below (``sg_repo``) intentionally unchanged
            # -- flipping the namespace would orphan existing bare-keyed
            # data; that decision needs its own ADR + migration.
            sg_branch = get_default_branch(repo_project)
            sg_filter = _load_symbol_graph_filter(
                repo_project, sg_branch, GITHUB_TOKEN or ""
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

    # Symbol-graph write + atomic-swap commit.
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
            # NOTE: _advance_gkg_sha used to fire here per-batch, which
            # caused the gkg ledger row to read N+0/1 instead of 1+0/1
            # (each batch increments completed_count past expected_count).
            # Functionally idempotent via the monotonic guard, but
            # operators scanning the ledger row would see numbers that
            # don't add up. Live discovery 2026-05-17. gkg advance now
            # fires from _mark_code_map_unit_done's "last close" branch
            # so it runs exactly once per (workspace, repo, commit_sha)
            # batch.
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

    # plan-git-truth chunk 02b: close one unit of the code_map_units
    # ledger row that process-repo-summary opened with expected_count =
    # the full fan-out size. When this close drives completed_count
    # equal to expected_count (the last unit), try_advance_sha checks
    # the paired code_map_dispatch stage and — if both are closed —
    # advances last_code_map_sha on the workspaces row.
    #
    # SHA-truth context requires repo_project (owner/repo), workspace_id,
    # commit_sha, AND commit_timestamp. Legacy / non-workspace producers
    # that omit any of them fall through harmlessly — the ledger row
    # never existed for that path.
    _mark_code_map_unit_done(batch, status="ok")

    # Optional cooldown period to prevent Bedrock throttling on next message.
    # fable-scale chunk 08: duration tfvar-driven (was hardcoded 10s). Default
    # 0 disables the cooldown entirely — enterprise Bedrock quotas absorb the
    # rate; low-quota accounts can dial it back up. Kept ENABLE_BEDROCK_COOLDOWN
    # as the master switch for backward compat.
    cooldown_ms = int(os.environ.get("BATCH_SUMMARY_MSG_COOLDOWN_MS", "0"))
    if ENABLE_BEDROCK_COOLDOWN and cooldown_ms > 0:
        cooldown_seconds = cooldown_ms / 1000.0
        logger.info(f"Waiting {cooldown_seconds}s cooldown before next message...")
        time.sleep(cooldown_seconds)


# ============================================================================
# Chunk 05: ledger-driven fan-in merge
# ============================================================================
#
# When a code unit is split into N shards, the splitter (chunk 03) also
# emits a companion merge message. The merge handler below reads the N
# shard summaries by their known vector keys, produces one unified
# summary via a single Bedrock call, and stores it at the parent vector
# key so downstream retrieval sees a single coherent summary per code
# unit -- not N partial ones.
#
# Merge messages may arrive before all shard summaries have landed
# (SQS delivery order is best-effort). The handler handles that by
# requeue-with-delay up to _MAX_MERGE_ATTEMPTS. If shards still haven't
# landed after the max attempts, the merge marks the ledger failed and
# gives up; the per-shard summaries still exist at their shard keys, so
# retrieval degrades gracefully to per-shard hits.
# ============================================================================


def _shard_vector_key(
    workspace_id: Optional[str],
    provider: str,
    repo: str,
    doc_type: str,
    parent_group_name: str,
    shard_index: int,
) -> str:
    """Compute the vector key for a single shard summary. Must match
    the key shape that ``store_batch_summary`` writes when it receives
    a shard message (which uses the shard-suffixed group_name that
    ``send_code_units_to_sqs`` set on the shard message). Keys on the BARE
    repo name (last path segment) to match store_batch_summary (ADR-052);
    without this, GitLab (slug repo) shard summaries would be written bare
    but fetched by a slug key, silently breaking merge fan-in. No-op for
    already-bare github/ADO."""
    repo_bare = repo.rsplit("/", 1)[-1]
    shard_group_name = f"{parent_group_name}#shard-{shard_index:02d}"
    if workspace_id:
        return f"{workspace_id}#{provider}#{repo_bare}#{doc_type}#{shard_group_name}"
    return f"{repo_bare}#{doc_type}#{shard_group_name}"


def _fetch_shard_summaries(
    shard_keys: List[str],
    expected_commit_sha: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch shard summaries from S3 Vectors by known key. Returns a
    list of {content, file_count, key} dicts for shards that have
    landed. Missing shards are silently dropped -- caller compares
    len(returned) to shard_count to decide whether to merge or requeue.

    When expected_commit_sha is set, shards whose metadata commit_sha
    doesn't match are treated as not-landed: shard keys are
    deterministic, so a previous run's shard vector sits at the same key
    as this run's and would otherwise satisfy the merge with stale
    content. Shards written before commit_sha stamping (no commit_sha
    in metadata) are likewise treated as stale.
    """
    if not shard_keys:
        return []
    try:
        response = s3vectors_client.get_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            keys=shard_keys,
            returnMetadata=True,
        )
    except ClientError as e:
        logger.warning(f"[merge] get_vectors failed: {e}")
        return []
    shards = []
    for vector in response.get("vectors", []):
        metadata = vector.get("metadata", {})
        content = metadata.get("content", "")
        if not content:
            continue
        if (
            expected_commit_sha
            and metadata.get("commit_sha") != expected_commit_sha
        ):
            logger.info(
                "[merge] Ignoring stale shard %s (commit_sha %s, "
                "expected %s)",
                vector.get("key", ""),
                metadata.get("commit_sha", "<absent>"),
                expected_commit_sha,
            )
            continue
        try:
            file_count = int(metadata.get("file_count", "0"))
        except (TypeError, ValueError):
            file_count = 0
        shards.append({
            "content": content,
            "file_count": file_count,
            "key": vector.get("key", ""),
        })
    return shards


def _build_merge_prompt(
    shard_summaries: List[Dict[str, Any]],
    parent_group_name: str,
    batch_type: str,
    repo: str,
) -> str:
    """Prompt Bedrock to unify N shard summaries into one coherent
    summary of the whole code unit. Preserves the shard-specific
    details (function names, file paths) while removing duplication
    and framing the unit as a single view."""
    numbered = "\n\n".join(
        f"[Shard {i + 1} of {len(shard_summaries)}]\n{s['content']}"
        for i, s in enumerate(shard_summaries)
    )
    return (
        f"The following are {len(shard_summaries)} partial summaries of "
        f"the {parent_group_name} {batch_type} in {repo}. Each covers a "
        f"disjoint subset of the code unit's files. Produce ONE unified "
        f"summary that:\n\n"
        "1. Captures the overall purpose and structure the shards "
        "collectively describe.\n"
        "2. Preserves the specific function names, file paths, and key "
        "relationships from each shard -- do not paraphrase away the "
        "concrete anchors.\n"
        "3. Removes duplication where multiple shards described the "
        "same convention or pattern.\n"
        "4. Reads as if authored from a single view of the whole code "
        "unit, not as concatenated fragments.\n\n"
        f"{numbered}\n\n"
        "Unified summary:"
    )


def _requeue_merge_with_delay(batch: Dict[str, Any], merge_attempt: int) -> bool:
    """Send the merge message back to CODE_MAPS_QUEUE with a delay so
    shard summaries have time to land. Returns True on success. Used
    when the merge handler finds fewer than shard_count summaries."""
    if not CODE_MAPS_QUEUE_URL:
        logger.warning(
            "[merge] CODE_MAPS_QUEUE_URL not configured; cannot requeue"
        )
        return False
    requeued = dict(batch)
    requeued["merge_attempt"] = merge_attempt
    try:
        sqs_client.send_message(
            QueueUrl=CODE_MAPS_QUEUE_URL,
            MessageBody=json.dumps(requeued),
            DelaySeconds=_MERGE_REQUEUE_DELAY_SECONDS,
        )
        return True
    except ClientError as e:
        logger.error(f"[merge] Failed to requeue merge message: {e}")
        return False


def handle_merge_message(batch: Dict[str, Any]) -> None:
    """Fan-in merge: read N shard summaries, produce one unified
    summary at the parent vector key.

    Requeue-with-delay when fewer than shard_count summaries have
    landed. After _MAX_MERGE_ATTEMPTS the merge gives up: per-shard
    summaries still exist at their shard-suffixed keys, so retrieval
    degrades to shard-level hits.
    """
    parent_group_name = batch.get("parent_group_name") or batch.get("group_name", "")
    shard_count = int(batch.get("shard_count", 0))
    workspace_id = batch.get("workspace_id") or ""
    provider = batch.get("provider", "github")
    repo = batch["repo"]
    batch_type = batch["batch_type"]
    doc_type = f"{batch_type}-summary"

    if shard_count <= 0:
        logger.warning(
            f"[merge] shard_count missing or zero on {parent_group_name}; "
            "nothing to merge -- marking ledger complete"
        )
        _mark_code_map_unit_done(batch, status="ok")
        return

    shard_keys = [
        _shard_vector_key(
            workspace_id=workspace_id or None,
            provider=provider,
            repo=repo,
            doc_type=doc_type,
            parent_group_name=parent_group_name,
            shard_index=i,
        )
        for i in range(shard_count)
    ]
    # Pass the merge message's commit_sha so stale shards from a
    # previous run (same deterministic keys) can't satisfy the merge
    # before this run's shards land.
    shard_summaries = _fetch_shard_summaries(
        shard_keys, expected_commit_sha=batch.get("commit_sha"),
    )

    if len(shard_summaries) < shard_count:
        merge_attempt = int(batch.get("merge_attempt", 0)) + 1
        missing = shard_count - len(shard_summaries)
        if merge_attempt >= _MAX_MERGE_ATTEMPTS:
            logger.error(
                f"[merge] Merge DLQ'd after {merge_attempt} attempts: "
                f"{parent_group_name} still missing {missing}/{shard_count} "
                "shards -- giving up; per-shard summaries remain at their "
                "shard-suffixed keys"
            )
            _mark_code_map_unit_done(
                batch,
                status="failed",
                failure_reason="merge_shards_missing",
                failure_detail=(
                    f"still missing {missing}/{shard_count} shards "
                    f"after {merge_attempt} attempts"
                ),
                attempt_count=merge_attempt,
            )
            return
        logger.info(
            f"[merge] {parent_group_name}: {len(shard_summaries)}/"
            f"{shard_count} shards ready; requeueing merge with "
            f"{_MERGE_REQUEUE_DELAY_SECONDS}s delay (attempt {merge_attempt})"
        )
        _requeue_merge_with_delay(batch, merge_attempt)
        # DO NOT mark ledger complete here -- the requeued message will
        # mark on its final iteration. Marking now would double-count
        # against expected_count and cause try_advance_sha to fire while
        # the merge is still pending.
        return

    # All shards ready -- do the actual merge.
    logger.info(
        f"[merge] {parent_group_name}: all {shard_count} shards ready; "
        "generating unified summary"
    )
    prompt = _build_merge_prompt(
        shard_summaries, parent_group_name, batch_type, repo,
    )
    try:
        merged_summary = _call_bedrock_and_audit(
            prompt, f"{parent_group_name} (merge of {shard_count} shards)",
        )
    except Exception as exc:
        logger.exception(
            f"[merge] Bedrock merge call failed for {parent_group_name}: {exc}"
        )
        _mark_code_map_unit_done(
            batch,
            status="failed",
            failure_reason="merge_bedrock_failure",
            failure_detail=str(exc)[:512],
        )
        raise

    # Build a merge-scoped batch dict so store_batch_summary writes at
    # the PARENT vector key (parent_group_name) and reports the union
    # file count across the shards.
    merge_batch = dict(batch)
    merge_batch["group_name"] = parent_group_name
    merge_batch["file_paths"] = [
        "*"
    ] * sum(s["file_count"] for s in shard_summaries)  # sizing hint only

    embedding = generate_embedding(merged_summary)
    if not store_batch_summary(merge_batch, merged_summary, embedding):
        raise Exception(
            f"Failed to store merged summary for {parent_group_name}"
        )

    _mark_code_map_unit_done(batch, status="ok")


def _mark_code_map_unit_done(
    batch: Dict[str, Any],
    *,
    status: str,
    failure_reason: str = "",
    failure_detail: str = "",
    attempt_count: int = 1,
) -> None:
    """Close one unit of the code_map_units ledger row for this
    batch's (workspace, repo, commit_sha) triple.

    ``status="ok"``      -> mark_unit_complete + try_advance_sha
    ``status="failed"``  -> mark_unit_failed + try_advance_sha

    Falls through silently when any of workspace_id / repo_project /
    commit_sha is absent from the batch (legacy / non-workspace
    code path; that producer never opened a SHA-truth ledger row
    so the consumer has nothing to mark).

    The advance is checked on both close paths so the LAST unit of
    the batch — successful or otherwise — drives ``last_code_map_sha``
    forward when the paired dispatch stage is also closed.
    """
    workspace_id = batch.get("workspace_id") or ""
    repo_project = batch.get("repo_project") or ""
    commit_sha = batch.get("commit_sha") or ""
    if not (workspace_id and repo_project and commit_sha):
        return

    try:
        from shared.sha_truth import (  # type: ignore
            mark_unit_complete,
            mark_unit_failed,
            try_advance_sha,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[process-batch-summary] sha_truth import failed: %s",
            exc,
        )
        return

    try:
        if status == "ok":
            ledger = mark_unit_complete(
                workspace_id, repo_project, "code_map_units", commit_sha
            )
        else:
            # Use the storage_key / group_name as the file_path label so
            # operators can locate which code unit dropped.
            file_path = (
                batch.get("storage_key")
                or batch.get("group_name")
                or "<unknown-code-unit>"
            )
            ledger = mark_unit_failed(
                workspace_id,
                repo_project,
                "code_map_units",
                commit_sha,
                file_path=str(file_path),
                failure_reason=failure_reason or "consumer_failed",
                failure_detail=failure_detail,
                attempt_count=attempt_count,
            )
        if ledger:
            try_advance_sha(ledger)
            # Determine whether THIS close is the last unit for the
            # (workspace, repo, commit_sha) triple -- the trigger for both
            # the once-per-regen gkg advance and the ADR-055 resolution pass.
            is_last_close = False
            try:
                expected = int(ledger.get("expected_count", 0))
                completed = int(ledger.get("completed_count", 0))
                failed = int(ledger.get("failed_count", 0))
                is_last_close = expected > 0 and (completed + failed) >= expected
            except Exception:  # pragma: no cover - defensive
                is_last_close = False

            if is_last_close:
                # Fire-once gkg advance: this batch IS the last one for the
                # triple. Synthesize the gkg ledger here so it runs exactly
                # once instead of per code unit. The gkg pointer table is the
                # symbol-graph correctness anchor; the workspaces
                # last_gkg_sha column is a mirror for the audit story, so
                # this best-effort failure mode is acceptable.
                try:
                    _advance_gkg_sha(workspace_id, repo_project, commit_sha)
                except Exception as gkg_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "gkg sha-truth advance failed for %s/%s@%s: %s "
                        "(pointer table is authoritative; workspaces "
                        "column will drift until next regen)",
                        workspace_id,
                        repo_project,
                        commit_sha,
                        gkg_exc,
                    )

                # ADR-055 whole-repo cross-module resolution post-pass. Only
                # on a FULL regen -- process-repo-summary stamps full_regen
                # on the batch messages when the incremental filter did not
                # reduce the code-unit set. An incremental sync re-extracts
                # only a subset of files, so the partition does not hold a
                # complete generation to resolve against (that between-regen
                # case is the ADR-055 best-effort incremental path, not built
                # here). Keyed on the BARE repo (batch["repo"]) to match the
                # symbol-graph table PK; _resolve_repo_symbol_graph is
                # best-effort and never raises.
                if batch.get("full_regen"):
                    sg_repo = batch.get("repo") or ""
                    if workspace_id and sg_repo and commit_sha:
                        _resolve_repo_symbol_graph(
                            workspace_id, sg_repo, commit_sha
                        )
                        # ADR-056 v2 producer-push: the producer's own graph is
                        # now resolved against the workspace index. Enqueue a
                        # cross-repo re-resolution of the repos that import it so
                        # their stale/raw cross-repo bindings refresh without a
                        # re-ingest. Async + best-effort; off the producer's
                        # critical path. Guarded by the same keys as above.
                        _enqueue_xrepo_reresolve(
                            workspace_id, sg_repo, commit_sha
                        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "[process-batch-summary] sha-truth %s failed for %s@%s: %s",
            status,
            repo_project,
            commit_sha,
            exc,
        )


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


def _advance_gkg_sha(workspace_id: str, repo: str, commit_sha: str) -> None:
    """Synthesize a single-unit gkg ledger entry + advance the
    ``last_gkg_sha`` column on the workspaces row.

    Plan-git-truth chunk 02: gkg is a single-writer pipeline with no
    fan-out, so we open + complete + advance in three sequential DDB
    calls to keep the audit contract uniform across pipelines. The
    pointer table (``shared.symbol_graph_pointer``) remains the
    correctness anchor; this just mirrors the SHA onto the workspaces
    row so the audit story has all three pipeline SHAs in one place.

    Commit timestamp falls back to wall-clock time — process-batch-summary
    runs serialized per-repo (Fargate consumer with MessageGroupId),
    so processing-time monotonicity is sufficient. If a future
    regulated buyer needs Git commit time on this column, thread the
    timestamp through the repo-summaries SQS message.
    """
    import time as _time

    from shared.sha_truth import (  # type: ignore
        PendingBatch,
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(PendingBatch(
        workspace_id=workspace_id,
        repo=repo,
        pipeline="gkg",
        target_sha=commit_sha,
        expected_count=1,
        target_commit_timestamp=int(_time.time()),
    ))
    ledger = mark_unit_complete(workspace_id, repo, "gkg", commit_sha)
    if ledger:
        try_advance_sha(ledger)


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

    # plan-git-truth chunk 02b: SQS maxReceiveCount on the code-maps
    # queue (terraform/sqs.tf) is 3. When a record's
    # ApproximateReceiveCount reaches that, the next failure DLQs it
    # invisibly. To preserve the SHA-truth contract (every code unit
    # eventually closes the ledger as complete OR failed), we detect
    # the last-attempt case here and write the failure to
    # code-map-failed-files + increment failed_count BEFORE
    # propagating the exception. Earlier-attempt failures still
    # propagate so SQS retries normally.
    _MAX_RECEIVE_COUNT = 3
    errors = []

    for record in event["Records"]:
        try:
            process_batch_record(record)
        except Exception as e:
            arc = int(
                record.get("attributes", {}).get(
                    "ApproximateReceiveCount", "1"
                )
            )
            logger.error(
                "Error processing batch (ARC=%d): %s",
                arc,
                e,
                exc_info=True,
            )
            if arc >= _MAX_RECEIVE_COUNT:
                # Last attempt — record a permanent failure and DO
                # NOT re-raise. The record completes at the SQS level
                # (no DLQ) but the SHA-truth ledger reflects the
                # permanent failure so try_advance_sha behaves
                # correctly.
                try:
                    batch = json.loads(record["body"])
                    _mark_code_map_unit_done(
                        batch,
                        status="failed",
                        failure_reason="consumer_exhausted_retries",
                        failure_detail=str(e)[:1024],
                        attempt_count=arc,
                    )
                except Exception as inner_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Failed to record permanent failure: %s",
                        inner_exc,
                    )
                continue
            errors.append(e)
            # Continue processing other records (existing behavior)

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
