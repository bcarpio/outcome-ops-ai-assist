# Architecture Overview

## High-Level Design

OutcomeOps applies **Context Engineering** to AI-assisted development: give AI access to your organizational knowledge (ADRs, code patterns, architectural decisions), and it generates code that already matches your standards.

### The Four-Phase Flow

**1. Ingest Phase**
- Scan repositories for ADRs, READMEs, code examples
- Generate embeddings using AWS Bedrock Titan v2
- Store in S3 Vectors for native similarity search

**2. Query Phase**
- Accept natural language query (from CLI, Chat UI, or Claude Code)
- Native vector search via S3 Vectors (cosine similarity)
- Retrieve top-K documents by similarity

**3. Generation Phase**
- Pass query + retrieved context to Claude Sonnet 4.5
- Claude generates code using YOUR patterns
- Returns implementation matching YOUR standards

**4. Review Phase**
- Automated PR checks validate against ADRs
- AI-powered review detects violations
- Human reviews for business logic only

### Why This Works

**Traditional AI coding:**
- AI generates generic code → You spend hours adapting it

**Context-engineered AI coding:**
- AI queries YOUR patterns → Generates YOUR standard code → You review outcomes only

**Result: 100-200x ROI, 16-hour tasks → 15 minutes**

---

## Detailed System Design

OutcomeOps AI Assist is a knowledge-driven code generation system that shifts development from task-oriented to outcome-oriented. It ingests your codebase patterns and architectural decisions, then uses Claude to generate code matching your exact conventions.

```
┌─────────────────────────────────────────────────────────────────┐
│              User Interfaces                                      │
│    (Chat UI, Claude Code, CLI, MS Teams, Slack)                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                ┌──────────┴──────────┐
                │                     │
                ▼                     ▼
        ┌───────────────┐    ┌──────────────────┐
        │  Query KB     │    │  Generate Code   │
        │  (RAG Search) │    │  (Code Maps)     │
        └───────┬───────┘    └────────┬─────────┘
                │                     │
                └──────────┬──────────┘
                           ▼
                   ┌───────────────┐
                   │  AWS Bedrock  │
                   │  Claude 4.5   │
                   └───────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
    ┌────────────┐    ┌─────────────┐   ┌─────────┐
    │ S3 Vectors │    │ DynamoDB    │   │    S3   │
    │(Embeddings)│    │(State Only) │   │(Docs)   │
    └────────────┘    └─────────────┘   └─────────┘
```

## Core Components

### 1. Knowledge Base Ingestion

**Lambda Function**: `ingest-docs` (runs hourly via EventBridge)

Scans your repositories via GitHub API and ingests documentation:

```
GitHub Repos (ADRs, READMEs, Docs)
         ↓
  [ingest-docs Lambda]
         ↓
Generate embeddings via Bedrock Titan v2
         ↓
Store in DynamoDB with metadata
         ↓
Archive in S3
         ↓
Available for queries + code generation
```

**What gets ingested:**
- ADRs from `docs/adr/` (all `.md` files)
- READMEs from repository roots
- Function-specific docs from `docs/lambda-*.md`, `docs/architecture.md`, etc.
- Code patterns via code maps (future)

**Storage architecture:**

*S3 Vectors (embeddings + content):*
```json
{
  "key": "adr#outcome-ops-ai-assist#ADR-001",   // Unique vector key
  "data": [0.123, 0.456, ...],                  // 1024-dimensional Titan v2 embedding
  "metadata": {
    "type": "adr",                              // Document type (filterable)
    "repo": "outcome-ops-ai-assist",            // Source repo (filterable)
    "content": "... full text ...",             // Document content (non-filterable)
    "file_path": "docs/adr/ADR-001.md",         // Original location (non-filterable)
    "content_hash": "abc123...",                // SHA-256 for change detection
    "timestamp": "2025-01-15T10:00:00Z"         // When ingested
  }
}
```

*DynamoDB (state tracking only):*
```json
{
  "PK": "repo#outcome-ops-ai-assist",           // Partition key
  "SK": "state#ingest",                         // Sort key
  "last_commit_sha": "abc123...",               // For incremental processing
  "last_processed": "2025-01-15T10:00:00Z"      // Timestamp
}
```

### 2. Vector Search (RAG)

Retrieval Augmented Generation pipeline for intelligent queries.

**Components:**
- Query embedding: Convert natural language to vector via Bedrock Titan v2
- Vector search: S3 Vectors native cosine similarity (no client-side computation)
- Result ranking: Top K most relevant documents with metadata filtering
- Context assembly: Pass top results to Claude

**Example flow:**
```
User query: "How should Lambda error handling work?"
         ↓
Generate embedding (Titan v2)
         ↓
Query S3 Vectors (native similarity search)
         ↓
Return top 5 documents (ADRs, code examples)
         ↓
Pass to Claude Sonnet 4.5 with context
         ↓
Claude generates answer grounded in YOUR patterns
```

**S3 Vectors advantages:**
- Native similarity search at database level (100-1000x faster than DynamoDB scan)
- No client-side cosine similarity calculations
- Metadata filtering for type/repo constraints
- Scales to millions of vectors

### 3. Event-Driven Test Automation

Once Claude finishes writing code, we need fast validation without waiting for GitHub Actions. OutcomeOps now uses an environment-scoped EventBridge bus (`${env}-${app}-bus`) to loosely couple code generation and testing:

1. `generate-code` emits `OutcomeOps.CodeGeneration.Completed` with repo, branch, PR info, and the environment/app identifiers.
2. An EventBridge rule on the same bus routes those events to the new `run-tests` Lambda.
3. `run-tests` pulls the latest commit, runs `make test` inside the runtime container, and publishes `OutcomeOps.Tests.Completed` (pass/fail plus S3 artifact pointers).
4. Future automation—PR commenters, deployment gates, Slack notifications—can subscribe to the same bus without changing the Lambdas.

This keeps the workflow serverless (no Step Functions required) while ensuring every generated branch ships with test results before CI picks it up.

### 3. Code Generation

Claude generates code using your patterns as context.

**In Claude Code IDE:**
1. You describe the outcome: "Create a Lambda handler for user updates"
2. Claude queries the knowledge base: finds error handling patterns, validation examples
3. Claude generates handler code: matching your conventions
4. You review for business logic; system ensures technical consistency

**Handled by:**
- Bedrock Claude Sonnet 4.5 model
- RAG context from knowledge base
- Your custom ADRs and patterns

### 4. Infrastructure Components

**AWS Services:**

| Component | Service | Purpose |
|-----------|---------|---------|
| **Compute** | Lambda | Serverless functions (ingest, query, generate, chat) |
| **Chat UI** | Fargate Spot + ALB | React frontend with streaming chat interface |
| **Knowledge Base** | Bedrock (Titan v2 + Claude 4.5) | Embeddings + LLM for generation |
| **Vector Storage** | S3 Vectors | Native similarity search with cosine distance |
| **State Tracking** | DynamoDB | Commit SHAs, processing state (no vectors) |
| **Document Archive** | S3 | Versioned storage of ingested documents |
| **Message Queues** | SQS FIFO | Decoupled processing (ingest, code-maps, PR checks) |
| **Configuration** | SSM Parameter Store | GitHub token, repo allowlist, service endpoints |
| **Encryption** | AWS KMS | Encrypt CloudWatch logs and SSM parameters |
| **Authentication** | ALB + OIDC | Azure AD integration for Chat UI access |
| **Scheduling** | EventBridge | Hourly ingestion and code-map triggers |
| **Container Registry** | ECR | Container images for run-tests and chat UI |
| **Infrastructure** | Terraform | IaC for all AWS resources |
| **Source Control** | GitHub API | Read-only repo access |
| **Monitoring** | CloudWatch | Logs, metrics, alarms |

**Network:**
- All AWS services in same region (no cross-region)
- Lambda to GitHub via public internet (GitHub API)
- Lambda to Bedrock via AWS service endpoint
- No public-facing endpoints (event-driven)

## Data Flow

### Ingestion Pipeline

```
1. EventBridge trigger
   └→ ingest-docs Lambda
      ├→ Load repos_allowlist from SSM
      ├→ For each repo:
      │  ├→ Fetch ADRs from GitHub API
      │  ├→ Fetch READMEs from GitHub API
      │  ├→ Fetch docs/* from GitHub API
      │  ├→ Generate embeddings via Bedrock
      │  ├→ Upload raw content to S3
      │  └→ Store metadata + embedding in DynamoDB
      └→ Return: { statusCode: 200, documents_ingested: N }
```

### Query Pipeline

```
1. User query: "How should I handle errors?"
   └→ vector-query Lambda (future)
      ├→ Generate embedding via Bedrock
      ├→ Search DynamoDB for top K results
      ├→ Pass results to Claude
      └→ Return: { answer: "...", sources: [...] }
```

### Code Generation Flow

```
1. User in Claude Code: "Create a Lambda handler"
   └→ Claude queries knowledge base via ask-claude Lambda
      ├→ Search for similar patterns
      ├→ Gather ADRs + code maps
      ├→ Pass context to Claude Sonnet
      └→ Return: Generated handler code
```

## Scaling Considerations

### Current Design (Per-Request)

- **Ingestion**: Hourly, ~5-10 documents
- **Queries**: On-demand via Lambda
- **Code generation**: Interactive in Claude Code

### Future Optimizations

1. **Batch processing**: SQS FIFO queue for large ingestions
2. **Vector search efficiency**: Dedicated vector database (Pinecone, Weaviate)
3. **Caching**: Redis for frequently accessed patterns
4. **Rate limiting**: Bedrock token limits (~100k/minute)

### Throttling Prevention

Current design prevents Bedrock throttling:
- Ingestion runs hourly (not continuous)
- Queries processed sequentially
- Text chunking limits tokens per document

If scaling beyond 100k tokens/minute:
1. Implement SQS FIFO queue
2. Batch chunks to respect rate limits
3. Add DLQ for failed batches
4. Monitor with CloudWatch metrics

## Security

### Data Protection

- **Encryption at rest**: S3 default encryption, DynamoDB point-in-time recovery
- **Encryption in transit**: HTTPS for GitHub API, AWS service endpoints
- **GitHub token**: Stored in SSM with KMS encryption

### Access Control

- **Lambda IAM role**: Least-privilege permissions per function
- **GitHub token scope**: Repository access only (not admin)
- **Bedrock models**: Region-specific ARN restrictions

### Monitoring

- **CloudWatch logs**: All Lambda execution details
- **Audit trail**: DynamoDB streams for data changes
- **Alarms**: Lambda errors, DynamoDB throttling

## Related Documentation

- **Lambda Functions**: See `docs/lambda-*.md` for specific function details
- **Deployment**: See `docs/deployment.md` for setup instructions
- **ADRs**: See `docs/adr/` for architectural decisions
- **Terraform**: See `terraform/` for infrastructure definitions
