# Security Overview

This document provides a comprehensive security overview of OutcomeOps AI Assist based on the actual implementation in the codebase.

## Deployment Model

OutcomeOps AI Assist is deployed **in the customer's own AWS account**. This is not a SaaS application. All data, compute, and infrastructure are owned and controlled by the customer. There is no shared infrastructure, no cross-customer data access, and no OutcomeOps-operated backend.

| Aspect | Description |
|--------|-------------|
| **Deployment** | Customer's AWS account |
| **Data Residency** | Customer-controlled (their chosen AWS region) |
| **Infrastructure** | Customer-owned and managed |
| **Access** | Customer's IAM, networking, and identity systems |
| **Billing** | Usage-based license; all AWS costs are the customer's |

---

## Architecture Overview

### AWS Region & Services

| Aspect | Configuration |
|--------|---------------|
| Primary Region | Customer-selected (default `us-west-2`) |
| Terraform State | S3 backend in customer's account |
| Deployment | Terraform IaC, CI/CD pipeline managed by customer |

### AWS Services Used

| Service | Purpose | Configuration |
|---------|---------|---------------|
| **ALB** | Application Load Balancer for UI and MCP servers | OIDC auth (Azure AD) for UI, API key auth for MCP, TLS 1.3 |
| **ECS Fargate** | Container hosting for UI proxy and MCP servers | Fargate Spot, private subnets |
| **Lambda** | Serverless compute | Python 3.12, various memory/timeout configs |
| **Lambda Function URL** | Streaming chat endpoint | IAM auth (SigV4), response streaming |
| **API Gateway v2** | GitHub webhook endpoint | HTTP API, CORS restricted to github.com |
| **DynamoDB** | State tracking, chat conversations | Pay-per-request, PITR enabled |
| **S3** | Knowledge base document storage | Versioning, AES-256, public access blocked |
| **S3 Vectors** | Vector embeddings | 1024-dim, cosine similarity, AES-256 |
| **SQS** | Async job processing | FIFO queues, DLQs, server-side encryption |
| **KMS** | CloudWatch Logs encryption | Auto-rotation enabled |
| **SSM Parameter Store** | Configuration and secrets | SecureStrings for sensitive values |
| **EventBridge** | Scheduled jobs, automation events | Hourly sync triggers |
| **Bedrock** | AI/ML | Titan Embeddings v2, Claude models |
| **ECR** | Container image registry | Scan on push, AES-256 encryption |
| **Cloud Map** | Service discovery for MCP servers | Internal DNS (`mcp.local`), multivalue routing |
| **ACM** | TLS certificates | DNS-validated, auto-renewed |
| **Route 53** | DNS management | FQDN for UI endpoint |

### Data Flow Summary

1. **Authentication**: User accesses UI via ALB -> Azure AD OIDC -> ALB forwards authenticated request to Fargate
2. **Chat Query**: Fargate proxy -> SigV4-signed request -> Lambda Function URL -> Bedrock LLM -> Streamed response
3. **Knowledge Base Search**: Query -> Titan embedding -> S3 Vectors cosine search -> Context retrieval
4. **Document Ingestion**: EventBridge (hourly) -> SQS -> Ingest Lambda -> S3 + S3 Vectors
5. **Code Generation**: GitHub webhook -> API Gateway -> Lambda -> SQS pipeline -> PR creation
6. **MCP Client Query**: MCP client -> API key auth -> ALB (bypasses OIDC) -> MCP Server (Fargate) -> DynamoDB/S3 Vectors/Bedrock
7. **MCP Tool Proxy**: MCP Server -> discovers downstream MCP servers -> proxies tool calls with SSM tokens or KMS-encrypted credentials

---

## Authentication & Authorization

### User Authentication: ALB OIDC (Azure AD)

OutcomeOps AI Assist uses AWS ALB's built-in OIDC integration with Azure AD (Microsoft Entra ID) for user authentication. There is no custom authentication code.

**Implementation**: `terraform/ui-fargate.tf` (lines 245-274)

#### Flow

1. User accesses the UI via HTTPS
2. ALB intercepts the request and checks for a valid OIDC session cookie
3. If no session, ALB redirects to Azure AD login
4. User authenticates with Azure AD (supports MFA, conditional access, etc.)
5. Azure AD returns tokens to ALB callback
6. ALB sets `AWSELBAuthSessionCookie-*` (HttpOnly, Secure) and forwards request to Fargate
7. ALB adds `x-amzn-oidc-data` (JWT) and `x-amzn-oidc-identity` headers

#### OIDC Configuration

```hcl
# terraform/ui-fargate.tf
authenticate_oidc {
  authorization_endpoint = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
  token_endpoint         = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
  user_info_endpoint     = "https://graph.microsoft.com/oidc/userinfo"
  issuer                 = "https://login.microsoftonline.com/{tenant_id}/v2.0"
  client_id              = var.oidc_client_id
  client_secret          = data.aws_ssm_parameter.oidc_client_secret[0].value
  scope                  = "openid email profile"
}
```

#### User Identity Extraction

The Fargate proxy extracts user identity from ALB OIDC headers:

```javascript
// ui/server/index.js
function extractUser(req) {
  const oidcData = req.headers["x-amzn-oidc-data"];
  if (!oidcData) {
    return { email: "anonymous", name: "anonymous" };
  }
  const payload = JSON.parse(
    Buffer.from(oidcData.split(".")[1], "base64url").toString()
  );
  return {
    email: payload.email || payload.sub || "unknown",
    name: payload.name || payload.email || "unknown",
  };
}
```

#### Session Management

| Parameter | Value | Description |
|-----------|-------|-------------|
| Session Storage | ALB-managed cookies | `AWSELBAuthSessionCookie-*` |
| Cookie Flags | HttpOnly, Secure | Not accessible via JavaScript |
| Session Duration | ALB default (configurable) | Controlled by ALB OIDC settings |
| MFA Support | Azure AD-managed | Customer configures in Entra ID |
| Logout | Cookie clearing + redirect | Server clears ALB session cookies |

#### Logout Implementation

```javascript
// ui/server/index.js
app.get("/api/logout", (req, res) => {
  for (let i = 0; i < 5; i++) {
    res.cookie(`AWSELBAuthSessionCookie-${i}`, "", {
      expires: new Date(0),
      path: "/",
      httpOnly: true,
      secure: true,
    });
  }
  res.redirect("/");
});
```

### MCP Client Authentication: API Keys

The MCP server exposes tools via the Streamable HTTP transport (JSON-RPC 2.0 over HTTP POST). MCP clients authenticate using API keys instead of OIDC. The ALB routes MCP traffic (`/mcp/v1*`) directly to the MCP server container, **bypassing the OIDC authentication rule**.

**Implementation**: `terraform/mcp-outcomeops.tf`, `lambda/mcp-server/handler.py`

#### API Key Format and Storage

| Aspect | Detail |
|--------|--------|
| Format | `ooa_` prefix + 40 hex characters (20 random bytes) |
| Storage | SHA-256 hash in DynamoDB (`GSI1PK: API_KEY#{hash}`) |
| Plaintext | Never stored; shown once at creation time |
| Expiration | Optional; enforced at validation time |
| Revocation | `status` field set to `revoked`; checked on every request |

#### Authentication Flow

1. MCP client sends `initialize` JSON-RPC request with `Authorization: Bearer ooa_xxxxx` header
2. MCP server hashes the key with SHA-256 and queries DynamoDB GSI
3. Server verifies the key is `active` and not expired
4. Server creates an in-memory session (32-char hex ID, 1-hour timeout) and returns `Mcp-Session-Id` header
5. Subsequent requests use the session ID header (or Bearer token) for authentication
6. Background thread cleans up expired sessions every 5 minutes

#### API Key Management

Users create and manage API keys via the UI Settings page:

| Operation | Endpoint | Auth |
|-----------|----------|------|
| Create key | `POST /api/api-keys` | OIDC (UI) |
| List keys | `GET /api/api-keys` | OIDC (UI) |
| Revoke key | `DELETE /api/api-keys/{key_id}` | OIDC (UI) |

Keys are scoped to the authenticated user's email. The `last_used_at` timestamp is updated on each MCP request.

#### ALB Routing (OIDC Bypass)

MCP paths are routed via ALB listener rules that do **not** include an OIDC authentication action:

| ALB Path | Priority | Target | Auth |
|----------|----------|--------|------|
| `/mcp/v1*` | 3 | OutcomeOps MCP server | API key (application-level) |
| `/mcp/snyk*` | 6 | Snyk MCP server | API key (application-level) |
| `/mcp*` | 7 | SonarQube MCP server | API key (application-level) |
| All other paths | Default | UI Fargate (with OIDC) | Azure AD OIDC |

This design is intentional: MCP clients (Claude Code, VS Code, Cursor) cannot participate in browser-based OIDC flows. Authentication is enforced at the application layer via API key validation.

### Service-to-Service Authentication: IAM + SigV4

Internal service communication uses AWS IAM with Signature Version 4:

| Source | Destination | Auth Method |
|--------|-------------|-------------|
| Fargate proxy | Lambda Function URL | SigV4-signed HTTPS |
| Lambda | DynamoDB, S3, Bedrock | IAM role credentials |
| Lambda | Lambda (invocation) | IAM role credentials |
| GitHub | API Gateway | Webhook signature (HMAC-SHA256) |
| MCP server | DynamoDB, S3 Vectors, Bedrock | IAM task role credentials |
| MCP server | Catalog MCP servers (SonarQube, Snyk) | SSM-stored tokens (custom headers) |
| MCP server | User-added MCP servers | KMS-encrypted credentials (Bearer or API key) |

**SigV4 Implementation** (`ui/server/index.js`):

```javascript
const signer = new SignatureV4({
  service: "lambda",
  region: AWS_REGION,
  credentials: defaultProvider(),
  sha256: Sha256,
});
```

### Webhook Authentication: HMAC-SHA256

GitHub webhooks are validated using HMAC-SHA256 signature verification:

```python
# lambda/generate-code/github_api.py
def get_webhook_secret() -> str:
    param_name = f"/{ENV}/{APP_NAME}/github/webhook-secret"
    response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
    return response.get("Parameter", {}).get("Value")
```

The webhook secret is stored as an SSM SecureString and retrieved at runtime with KMS decryption.

### User Scoping

Chat conversations are scoped to individual users via the `user_id` field (derived from OIDC email):

```python
# lambda/chat/handler.py
user_id = event.get("headers", {}).get("x-user-email", "")
conversations = list_conversations(user_id=user_id)
```

Users can only see and manage their own conversations.

### MCP Server Proxy Security

The OutcomeOps MCP server acts as a gateway that discovers and proxies tools from downstream MCP servers (SonarQube, Snyk, and user-added servers). This proxy layer has its own security model.

#### Tool Discovery and Namespacing

When a client calls `tools/list`, the MCP server discovers available downstream servers by querying DynamoDB for workspace-to-server assignments and global catalog servers. Tools from downstream servers are namespaced to prevent collisions:

| Server | Original Tool | Exposed As |
|--------|--------------|------------|
| SonarQube | `search_issues` | `sonarqube__search_issues` |
| Snyk | `list_organizations` | `snyk__list_organizations` |

Tool routing is stored per-session. Clients can only invoke tools from servers assigned to their accessible workspaces.

#### Credential Isolation for Downstream Servers

| Server Type | Credential Source | Mechanism |
|-------------|------------------|-----------|
| Catalog servers (SonarQube, Snyk) | SSM Parameter Store | Token fetched from SSM, sent as custom header |
| User-added servers (Bearer auth) | DynamoDB | KMS-encrypted credentials decrypted at call time |
| User-added servers (API key auth) | DynamoDB | KMS-encrypted credentials decrypted at call time |
| No-auth servers | N/A | Request sent without credentials |

Credentials for user-added MCP servers are encrypted with a dedicated KMS key (`kms-oauth.tf`) before storage in DynamoDB and decrypted only when proxying a tool call.

#### Session Management

| Parameter | Value |
|-----------|-------|
| Session storage | In-memory (per-process) |
| Session ID format | 32-character hex token (`secrets.token_hex(16)`) |
| Session timeout | 1 hour (3600 seconds) |
| Cleanup interval | Every 5 minutes (background thread) |
| Downstream session TTL | 30 minutes (reused across tool calls) |
| Tool cache TTL | 5 minutes (per-server, in-memory + DynamoDB fallback) |

Sessions are not persisted across container restarts. If a Fargate task is replaced (Spot interruption, deployment), clients must re-authenticate.

---

## Data Security

### Storage Locations

| Data Type | Service | Key Pattern |
|-----------|---------|-------------|
| Knowledge base docs | S3 | `{repo_owner}/{repo_name}/{path}/{file}` |
| Vector embeddings | S3 Vectors | `{repo}#{path}#chunk{N}` |
| Conversation memory | S3 Vectors | `conv#{conversation_id}#memory#{chunk_id}` |
| State tracking | DynamoDB | `PK: REPO#{repo}`, `SK: STATE#{type}` |
| Chat conversations | DynamoDB | `PK: CONV#{conversation_id}`, `SK: METADATA \| MSG#{ts}#{id}` |
| API key hashes | DynamoDB | `PK: USER#{email}`, `SK: API_KEY#{key_id}`, `GSI1PK: API_KEY#{hash}` |
| MCP server metadata | DynamoDB | `PK: MCP#{mcp_id}`, `SK: METADATA` |
| MCP server tools cache | DynamoDB | `PK: MCP#{mcp_id}`, `SK: TOOLS` |
| MCP workspace assignments | DynamoDB | `PK: WS#{workspace_id}`, `SK: MCP_SERVER#{mcp_id}` |
| MCP encrypted credentials | DynamoDB | Stored in MCP server metadata, KMS-encrypted |
| Configuration | SSM Parameter Store | `/{env}/{app_name}/{category}/{key}` |
| Container images | ECR | `{env}-{app_name}-{service}` |

### Encryption

#### At Rest

| Service | Encryption | Key Management |
|---------|------------|----------------|
| DynamoDB (code-maps) | AES-256 | AWS-managed |
| DynamoDB (chat) | AES-256 | AWS-managed |
| S3 (knowledge base) | SSE-S3 (AES-256) | AWS-managed |
| S3 Vectors | AES-256 | AWS-managed |
| SQS (all queues) | SQS-managed SSE | AWS-managed |
| ECR (all repos) | AES-256 | AWS-managed |
| CloudWatch Logs | KMS | Customer-managed key (auto-rotation) |
| SSM SecureString | KMS | AWS-managed CMK (`alias/aws/ssm`) |

**CloudWatch Logs KMS Key** (`terraform/kms.tf`):

```hcl
resource "aws_kms_key" "cloudwatch_logs" {
  enable_key_rotation = true
  policy = jsonencode({
    Statement = [
      {
        Sid       = "Allow CloudWatch Logs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:..." }
        }
      }
    ]
  })
}
```

#### In Transit

All connections use HTTPS with TLS 1.2 minimum:

| Connection | TLS Version | Certificate |
|------------|-------------|-------------|
| User -> ALB | TLS 1.3 (`ELBSecurityPolicy-TLS13-1-2-2021-06`) | ACM |
| MCP Client -> ALB | TLS 1.3 (`ELBSecurityPolicy-TLS13-1-2-2021-06`) | ACM |
| ALB -> MCP Server (Fargate) | HTTP (internal, within VPC) | N/A |
| MCP Server -> Downstream MCP Servers | HTTP (internal, via Cloud Map DNS) | N/A |
| Fargate -> Lambda Function URL | TLS 1.2+ | AWS internal |
| Lambda -> AWS services | TLS 1.2+ | AWS internal |
| Lambda -> GitHub API | TLS 1.2+ | GitHub-managed |
| GitHub -> API Gateway | TLS 1.2+ | AWS-managed |

### Data Isolation

This platform uses a **single-tenant deployment model**. Each customer gets their own AWS account deployment with complete infrastructure isolation:

| Isolation Layer | Mechanism |
|-----------------|-----------|
| Account-level | Each customer deploys in their own AWS account |
| Network-level | Customer's VPC, subnets, security groups |
| Identity-level | Customer's Azure AD tenant |
| Encryption-level | Customer's KMS keys, customer-managed |
| User-level | OIDC email-based scoping for chat conversations |

There is no multi-tenant data commingling. All data resides in the customer's AWS account.

### DynamoDB Configuration

| Table | PITR | Deletion Protection | Streams | Encryption |
|-------|------|---------------------|---------|------------|
| Code Maps | Yes | No | Yes (NEW_AND_OLD_IMAGES) | AES-256 |
| Chat | Yes | No | No | AES-256 |

---

## Integrations

### GitHub Integration

GitHub is integrated using a Personal Access Token (PAT), not OAuth:

| Aspect | Configuration |
|--------|---------------|
| Auth Method | Personal Access Token (classic or fine-grained) |
| Token Storage | SSM SecureString (`/{env}/{app_name}/github/token`) |
| Retrieval | `ssm:GetParameter` with `WithDecryption=True` |
| Webhook Secret | SSM SecureString (`/{env}/{app_name}/github/webhook-secret`) |
| Webhook Validation | HMAC-SHA256 signature verification |

**No OAuth flow, no token rotation complexity.** The customer creates a PAT in GitHub with appropriate scopes and stores it in SSM. The platform retrieves it at runtime via IAM-authorized SSM access.

### Jira Integration (Optional)

When enabled (`enable_jira_integration = true`), Jira connects via Atlassian Automation with cross-account IAM:

| Aspect | Configuration |
|--------|---------------|
| Auth Method | Cross-account IAM role assumption |
| Trust | Atlassian AWS account (815843069303) with external ID |
| Permissions | SSM read, Lambda invoke (scoped to specific resources) |

```hcl
# terraform/ssm_jira_automation.tf
assume_role_policy = jsonencode({
  Statement = [{
    Effect    = "Allow"
    Principal = { AWS = "arn:aws:iam::815843069303:root" }
    Action    = "sts:AssumeRole"
    Condition = {
      StringEquals = { "sts:ExternalId" = var.jira_external_id }
    }
  }]
})
```

### Bedrock Models

| Model | Purpose | Access |
|-------|---------|--------|
| Amazon Titan Embed Text v2 | Vector embeddings (1024-dim) | IAM-authorized |
| Claude 3.5 Haiku | Standard chat (fast) | IAM-authorized, cross-region inference |
| Claude Sonnet 4.5 | Advanced chat (better quality) | IAM-authorized, cross-region inference |

Model access is granted through specific ARN-scoped IAM policies, not blanket Bedrock access.

---

## Infrastructure Security

### Lambda Configuration

| Lambda | Memory | Timeout | Purpose |
|--------|--------|---------|---------|
| chat-streaming | 1 GB | 900s | Streaming RAG chat (LWA) |
| ingest-docs | 512 MB | 900s | Document ingestion from GitHub |
| generate-code-maps | 512 MB | 900s | Code map generation |
| generate-code | 512 MB | 900s | AI code generation pipeline |
| query-kb | 256 MB | 30s | Knowledge base queries |
| ask-claude | 256 MB | 120s | Direct Claude queries |
| analyze-pr | 256 MB | 120s | PR analysis |
| process-pr-check | 256 MB | 600s | PR check processing |
| run-tests | 1 GB | 900s | Container-based test execution |
| process-batch-summary | 512 MB | 300s | Repository summaries |

All Lambda functions have:
- CloudWatch Logs with 7-day retention
- KMS-encrypted log groups
- Least-privilege IAM policies

### SQS Queue Configuration

All queues are FIFO with server-side encryption and dead letter queues:

| Queue | Visibility Timeout | Max Retries | DLQ |
|-------|-------------------|-------------|-----|
| ingest-docs | 900s | 3 | Yes |
| code-maps | 900s | 3 | Yes |
| pr-checks | 720s | 3 | Yes |
| code-generation | 900s | 4 | Yes |
| repo-summaries | 600s | 3 | Yes |

### S3 Bucket Security

| Bucket | Versioning | Public Access | Encryption |
|--------|------------|---------------|------------|
| Knowledge Base | Enabled | All blocked | SSE-S3 (AES-256) |

**S3 Bucket Policy** (`terraform/s3.tf`):
- `block_public_acls = true`
- `block_public_policy = true`
- `ignore_public_acls = true`
- `restrict_public_buckets = true`

### ECR Container Security

| Repository | Image Tag Mutability | Scan on Push | Encryption |
|------------|---------------------|--------------|------------|
| run-tests | Immutable | Yes | AES-256 |
| chat-ui | Mutable | Yes | AES-256 |
| mcp-server (outcomeops) | Mutable | Yes | AES-256 |
| mcp-snyk | Mutable | Yes | AES-256 |

Lifecycle policies retain the last 5-10 images.

### Network Security

#### ALB Security Group

```hcl
# Internal mode: VPC CIDR only
# Public mode: 0.0.0.0/0 (secured by OIDC at ALB)
ingress: 80/tcp, 443/tcp from VPC CIDR or 0.0.0.0/0
egress: all
```

#### UI Fargate Security Group

```hcl
# Only accepts traffic from ALB
ingress: 3000/tcp from ALB security group only
egress: all
```

#### MCP Server Security Groups

Each MCP server (OutcomeOps, SonarQube, Snyk) has a dedicated security group:

```hcl
# Only accepts traffic from ALB on MCP port
ingress: 8080/tcp from ALB security group only
egress: all
```

**Security posture**:
- All Fargate tasks (UI and MCP) are only reachable via ALB
- UI paths enforce OIDC authentication at the ALB
- MCP paths enforce API key authentication at the application layer
- No direct internet access to containers
- HTTP automatically redirected to HTTPS (301)

#### Lambda Functions

Lambda functions run in the AWS-managed VPC (not customer VPC). They access AWS services via the AWS network backbone.

### Fargate Container Security

#### UI Proxy Container

| Control | Configuration |
|---------|---------------|
| Read-only root filesystem | `readonlyRootFilesystem = true` |
| Minimum resources | 0.25 vCPU, 512 MB RAM |
| Capacity provider | Fargate Spot |
| Health check | HTTP GET `/health` every 30s |
| Task role | Scoped to `lambda:InvokeFunctionUrl` only |

#### MCP Server Containers

| Control | Configuration |
|---------|---------------|
| Resources | 0.5 vCPU, 1 GB RAM per server |
| Capacity provider | Fargate Spot |
| Networking | Private subnets only (no public IP) |
| Health check | HTTP GET `/health` on port 8080 |
| Service discovery | Cloud Map DNS (`{name}-mcp.mcp.local`, TTL 10s) |
| Task role | DynamoDB, S3 Vectors, Bedrock, SSM read, KMS decrypt |
| Secrets injection | SSM Parameter Store values injected at task startup |

### CloudWatch Logs

| Log Source | Retention | Encryption |
|------------|-----------|------------|
| Lambda functions | 7 days | KMS (customer-managed key) |
| Fargate tasks | Auto-created by ECS | Default |

---

## Secret Management

All secrets are managed through AWS Systems Manager Parameter Store. No secrets are stored in code, environment variables, or configuration files.

### Secrets Inventory

| Secret | SSM Path | Type |
|--------|----------|------|
| GitHub PAT | `/{env}/{app_name}/github/token` | SecureString |
| GitHub Webhook Secret | `/{env}/{app_name}/github/webhook-secret` | SecureString |
| OIDC Client Secret | `/{env}/{app_name}/oidc/client-secret` | SecureString |
| License Key (optional) | `/{env}/{app_name}/license/key` | SecureString |
| SonarQube MCP Token | `/{env}/{app_name}/mcp/sonarqube-token` | SecureString |
| Snyk MCP Token | `/{env}/{app_name}/mcp/snyk-token` | SecureString |

### Configuration Parameters

Non-sensitive configuration is stored as regular SSM `String` parameters:

| Parameter | SSM Path | Purpose |
|-----------|----------|---------|
| KB Bucket | `/{env}/{app_name}/s3/knowledge-base-bucket` | S3 bucket name |
| Vector Bucket | `/{env}/{app_name}/s3vectors/bucket-name` | Vector store name |
| Vector Index | `/{env}/{app_name}/s3vectors/index-name` | Vector index name |
| Code Maps Table | `/{env}/{app_name}/dynamodb/code-maps-table` | DynamoDB table name |
| Chat Table | `/{env}/{app_name}/dynamodb/chat-table` | DynamoDB table name |
| Queue URLs | `/{env}/{app_name}/sqs/*-queue-url` | SQS queue URLs |
| Streaming URL | `/{env}/{app_name}/api/chat-streaming-url` | Lambda Function URL |

### Secret Access Pattern

```python
# All Lambdas use this pattern:
response = ssm_client.get_parameter(Name=param_name, WithDecryption=True)
secret = response["Parameter"]["Value"]
```

Lambda IAM policies restrict SSM access to the application's parameter path prefix and KMS decrypt to the AWS-managed SSM key only.

---

## CORS Configuration

### Lambda Function URL

```hcl
cors {
  allow_origins     = ["*"]
  allow_methods     = ["POST", "GET", "DELETE"]
  allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
  allow_credentials = false
  max_age           = 300
}
```

The Lambda Function URL uses `AWS_IAM` authorization, so CORS is secondary to IAM auth. Only the Fargate task role can invoke the function.

### API Gateway (GitHub Webhooks)

```hcl
cors_configuration = {
  allow_headers = ["*"]
  allow_methods = ["POST"]
  allow_origins = ["https://github.com"]
}
```

Restricted to GitHub's origin. Additionally, webhooks are validated via HMAC-SHA256 signature.

---

## Input Validation

### Webhook Validation

GitHub webhook payloads are validated using:
1. HMAC-SHA256 signature verification (shared secret from SSM)
2. Pydantic model validation for event structure
3. Label allowlisting (`approved-for-generation`, `approved-for-plan` only)
4. Action type restriction (`labeled` only)

```python
# lambda/generate-code/models.py
VALID_LABELS = ["approved-for-generation", "approved-for-plan"]
VALID_ACTIONS = ["labeled"]
```

### Chat Input

```python
# lambda/chat/handler.py
class ChatMessageRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    top_k: Optional[int] = 5
    advanced: Optional[bool] = False
    show_cost: Optional[bool] = False
```

### Express.js Proxy

```javascript
// Body size limit
app.use(express.json({ limit: "1mb" }));
```

---

## Security Principles

The following security principles are enforced throughout the codebase:

1. **Customer-deployed**: All data stays in the customer's AWS account. No data leaves to OutcomeOps infrastructure.
2. **Auth at the edge**: OIDC enforced at the ALB for UI; API key auth enforced at the application layer for MCP (ALB routes MCP traffic without OIDC).
3. **SigV4 internal auth**: Service-to-service communication uses IAM with SigV4 signing, not shared secrets.
4. **Least-privilege IAM**: Each Lambda has granular IAM policies scoped to specific resource ARNs.
5. **Secrets in SSM**: All secrets stored in SSM SecureString with KMS encryption. No secrets in code or environment variables.
6. **Encryption everywhere**: All data encrypted at rest (AES-256/KMS) and in transit (TLS 1.2+).
7. **Read-only containers**: Fargate tasks run with read-only root filesystem.
8. **DLQ for failures**: All async queues have dead letter queues preserving failed jobs.
9. **FIFO ordering**: All SQS queues are FIFO to prevent duplicate processing.
10. **Image scanning**: ECR scans container images on push for vulnerabilities.
11. **Webhook verification**: GitHub webhooks validated via HMAC-SHA256 before processing.
12. **User scoping**: Chat conversations are scoped to authenticated users via OIDC identity.
13. **API key hashing**: MCP API keys stored as SHA-256 hashes; plaintext never persisted.
14. **MCP credential isolation**: Downstream MCP server credentials encrypted with KMS or stored in SSM SecureString; decrypted only at proxy time.

---

## CI/CD Security

### Security Scanning

The CI/CD pipeline includes automated security checks at multiple layers:

| Tool | Purpose | Scope |
|------|---------|-------|
| **Snyk** | Dependency vulnerability scanning, license compliance | All Lambda `requirements.txt`, transitive dependencies |
| **SonarCloud** | Static code analysis, security hotspots, code quality gates | All application code (Python, TypeScript) |
| **ECR Scan-on-Push** | Container image layer scanning | Fargate and Lambda container images |

All code undergoes static analysis via SonarCloud and dependency/vulnerability scanning via Snyk as part of the CI/CD pipeline. No code ships with known medium+ CVSS vulnerabilities -- builds are blocked by pipeline quality gates.

### Pre-Commit Checks

All changes must pass before committing:

```bash
# Unit tests
make test-unit

# Terraform formatting and validation
cd terraform && terraform fmt -recursive && terraform validate

# Python compilation check
cd lambda && find . -name "*.py" -path "*/lambda/*" | head -20 | xargs -I {} python -m py_compile {}
```

### Pipeline Gates

All security scans must pass before:
- Lambda packages are built
- Terraform plans are applied
- Container images are pushed to ECR

### Infrastructure Deployment

- Terraform is the sole deployment mechanism
- `terraform apply` is only run via CI/CD pipeline, never locally
- State is stored in an encrypted S3 backend with DynamoDB locking
- All infrastructure changes are peer-reviewed via pull requests

### Container Image Build

- Container images are built in CI and pushed to ECR
- ECR scans images on push for known vulnerabilities
- Immutable tags for Lambda container images prevent tampering
- Snyk monitors container base images for newly disclosed vulnerabilities

---

## Customer Responsibilities

Because this platform is deployed in the customer's AWS account, the customer is responsible for:

| Responsibility | Description |
|----------------|-------------|
| **AWS Account Security** | IAM policies, CloudTrail, GuardDuty, Security Hub |
| **Network Security** | VPC configuration, NACLs, VPN/Direct Connect |
| **Azure AD Configuration** | MFA policies, conditional access, user provisioning |
| **Secret Rotation** | Rotating GitHub PAT, OIDC client secret as needed |
| **Backup & Recovery** | DynamoDB PITR, S3 versioning, disaster recovery planning |
| **Compliance** | Meeting regulatory requirements (SOC 2, HIPAA, etc.) |
| **Monitoring** | CloudWatch alarms, SNS notifications, incident response |
| **Patching** | Updating Terraform modules, Lambda runtimes, container base images |
| **Access Control** | Managing who has access to the AWS account and Azure AD |

---

## Comparison with SaaS Model

For context, here is how this customer-deployed model differs from a typical SaaS deployment:

| Aspect | OutcomeOps (Customer-Deployed) | Typical SaaS |
|--------|-------------------------------|--------------|
| Data location | Customer's AWS account | Vendor's infrastructure |
| Authentication | Customer's Azure AD | Vendor-managed (magic links, OAuth) |
| Multi-tenancy | Not needed (single tenant) | Required (org/workspace isolation) |
| Billing integration | Usage-based license key | Stripe/billing in application |
| OAuth integrations | PAT tokens (simpler) | Full OAuth flows (required for SaaS) |
| Infrastructure access | Customer has full AWS access | Vendor-managed, no customer access |
| Compliance scope | Customer's existing compliance posture | Vendor must achieve certifications |
| Secret management | Customer's SSM/KMS | Vendor's secret management |
| Incident response | Customer's SOC/SRE team | Vendor's incident response |
