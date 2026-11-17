# OutcomeOps MCP Server

Connect Claude Code, VS Code, Cursor, and other MCP-compatible tools to your
organization's knowledge base. Get coding standards, ADRs, and architectural
patterns directly in your development environment.

## How It Works

The OutcomeOps MCP server acts as a **gateway**. When you connect your MCP
client, it automatically discovers all MCP servers available to your workspaces
and exposes their tools alongside the native knowledge base tools. One
connection, one API key, all tools.

```
Developer's Machine                OutcomeOps MCP Gateway (Fargate)
+-------------------+             +-------------------------------------+
| Claude Code       |  HTTPS      | handler.py                          |
| MCP Client        | ---------> | 1. Auth (API key)                   |
| url: /mcp/v1      |  Bearer     | 2. tools/list -> native + proxied   |
| One connection    |  token      | 3. tools/call -> route to correct   |
+-------------------+             |    server via internal ALB           |
                                  +--------+----------------------------+
                                           |
                      +--------------------+--------------------+
                      |                    |                    |
                      v                    v                    v
               Native Tools         SonarQube MCP        Future MCP
               (KB query, etc.)     (ALB /mcp)           (ALB /mcp/X)
```

## Quick Start

### Step 1: Generate an API Key

1. Log into the OutcomeOps web UI
2. Navigate to the Workspaces page and click the **Settings** (gear) icon, or go to `/settings`
3. Click **Create API Key**
4. Give it a name (e.g., "Claude Code - Laptop")
5. Optionally set an expiration
6. Copy the key immediately -- it will not be shown again

### Step 2: Configure Your MCP Client

#### Claude Code

Run this command in your terminal:

```bash
claude mcp add --transport http outcomeops \
  https://your-outcomeops-domain.com/mcp/v1 \
  --header "Authorization: Bearer ooa_your_key_here"
```

#### VS Code (Copilot / GitHub MCP)

Add to your VS Code settings or `.vscode/mcp.json`:

```json
{
  "servers": {
    "outcomeops": {
      "type": "streamableHttp",
      "url": "https://your-outcomeops-domain.com/mcp/v1",
      "headers": {
        "Authorization": "Bearer ooa_your_key_here"
      }
    }
  }
}
```

#### Cursor

Add to your Cursor MCP configuration (Settings > MCP Servers):

- **Name**: OutcomeOps
- **Transport**: Streamable HTTP
- **URL**: `https://your-outcomeops-domain.com/mcp/v1`
- **Headers**: `Authorization: Bearer ooa_your_key_here`

#### Generic MCP Client

Any MCP client that supports Streamable HTTP transport can connect using:

- **URL**: `https://your-outcomeops-domain.com/mcp/v1`
- **Method**: POST (JSON-RPC 2.0)
- **Header**: `Authorization: Bearer ooa_your_key_here`
- **Protocol Version**: `2025-03-26`

### Step 3: Verify the Connection

After configuring your MCP client, try asking a question that uses the knowledge base:

> "What are our Lambda handler standards?"

The MCP client should call the `query_knowledge_base` tool and return results from
your organization's ADRs and documentation.

## Available Tools

### Native Tools (always available)

These tools are built into the OutcomeOps MCP server and always available to
authenticated users.

### query_knowledge_base

Search the knowledge base for organizational standards, ADRs, code patterns, and documentation.

**Parameters:**
- `query` (required): The question or topic to search for
- `workspace_id` (optional): Scope the search to a specific workspace

**Example:** `"What are our Terraform module standards?"`

### list_workspaces

List workspaces the authenticated user has access to. Returns workspace IDs and names.

**Parameters:** None

### list_workspace_documents

List documents ingested into a workspace's knowledge base.

**Parameters:**
- `workspace_id` (required): The workspace ID to list documents for

### get_coding_standards

Convenience wrapper that formulates queries optimized for retrieving coding standards.

**Parameters:**
- `topic` (required): The topic (e.g., "Lambda handlers", "Terraform modules", "testing")
- `workspace_id` (optional): Scope the search to a specific workspace

### Symbol-graph query tools

These tools answer structural questions about the source code in a workspace's
repositories. They are backed by the symbol-graph DynamoDB table populated by
the `process-batch-summary` Lambda's extractor pass during code-map regen.
Each tool requires a `workspace_id` the caller is authorized for; results are
scoped to that workspace. See `docs/features.md` (Structural retrieval section)
for the user-facing description and `docs/symbol-graph-validation.md` for the
operator runbook.

Responses are JSON-encoded text with the following shape:

```json
{
  "sites": [
    {
      "file_path": "lambda/github-integration/handler.py",
      "line": 137,
      "repo": "owner/repo",
      "source_symbol_id": "python:lambda.github_integration.handler::lambda_handler",
      "language": "python"
    }
  ],
  "truncated": false,
  "graph_sha": "abc123...",
  "staleness_seconds": 42
}
```

`truncated` is `true` when the result hit the `limit` cap with more
matches available. `graph_sha` is the commit SHA the answer was anchored
on (null for multi-repo answers). `staleness_seconds` is the worst-case
age of the underlying graph slices.

#### find_callers

Find every call-site of a symbol across the workspace. Answers questions like
"Which Lambdas call `log_audit_entry`?"

**Parameters:**
- `symbol_id` (required): Canonical symbol ID of the target
- `workspace_id` (required): Workspace to scope the query to
- `limit` (optional, default 200): Maximum number of call-sites to return

#### find_importers

Find every importer of a module or package. Accepts an exact subpath
(`@acme-corp/shared-utils/http2`) for exact match, or a package prefix
(`@acme-corp/shared-utils` or `@acme-corp/shared-utils/*`) for all consumers
of any subpath.

**Parameters:**
- `target_or_prefix` (required): Exact symbol or package prefix
- `workspace_id` (required): Workspace to scope the query to
- `limit` (optional, default 500): Maximum number of importer sites to return

#### find_implementers

Find every implementer of an interface, abstract class, or protocol.
Answers "What classes implement `IBookingClient`?"

**Parameters:**
- `interface_id` (required): Canonical symbol ID of the interface
- `workspace_id` (required): Workspace to scope the query to
- `limit` (optional, default 100): Maximum number of implementer sites to return

#### find_definitions

Find every definition site of a symbol. Returns a list so cross-repo
duplicates (interface re-declarations, copy-pasted code) all surface;
an empty list means "not found" -- never null.

**Parameters:**
- `symbol` (required): Canonical symbol ID to look up
- `workspace_id` (required): Workspace to scope the query to

#### find_symbol_by_name

Resolve a user-typed bare name (e.g., `require_auth`) to the set of
canonical symbol IDs that match it. The four directional tools above
all require canonical IDs; this tool maps bare names to canonicals
so an MCP client that only has the local function name (the user
typed it, or it came out of a model) can chain into them. Backed by
the `target-name-index` GSI for single-partition reads bounded by
actual matches.

Response shape:

```json
{
  "canonicals": [
    "python:lambda.auth.handler::require_auth",
    "python:lambda.org-management.handler::require_auth"
  ]
}
```

Typical client pattern: call `find_symbol_by_name(bare_name)` first,
then call `find_callers(canonical)` for each returned canonical and
merge the results. The chat handler does the equivalent fan-out
internally for in-chat questions; external MCP clients (Claude Code,
Cursor, etc.) opt in via this tool.

**Parameters:**
- `bare_name` (required): Function or class name as a user would
  type it -- no language prefix, namespace, or package
- `workspace_id` (required): Workspace to scope the query to
- `limit` (optional, default 25): Maximum distinct canonicals to
  return

### Proxied Tools (based on workspace access)

Tools from MCP servers assigned to your workspaces are automatically available.
They appear with a server prefix (e.g., `sonarqube__search_issues`).

Which proxied tools you see depends on:
- Which workspaces you are a member of
- Which MCP servers are assigned to those workspaces
- Whether any MCP servers are marked as global (available to all users)
- Workspaces shared with yours (via cross-workspace sharing)

Proxied tools are namespaced with the server name to avoid collisions. For
example, a SonarQube server exposes `sonarqube__search_issues` and
`sonarqube__get_metrics`. The tool list refreshes automatically as MCP servers
are added or removed from your workspaces.

## API Key Management

### Creating Keys

1. Go to **Settings** (gear icon on the Workspaces page, or `/settings`)
2. Click **Create API Key**
3. Enter a descriptive name (e.g., "Claude Code - Work Laptop")
4. Choose an expiration (recommended: 90 days for shared environments)
5. Copy the key immediately

### Revoking Keys

1. Go to **Settings** (gear icon on the Workspaces page)
2. Click the trash icon next to the key
3. Confirm revocation

Revoked keys are immediately invalidated. Any MCP client using the key will receive 401 errors.

### Security Best Practices

- Use one key per device/tool
- Set expiration dates, especially for shared environments
- Revoke immediately if a key is compromised
- Do not commit keys to version control
- Keys are stored as SHA-256 hashes -- plaintext keys are never stored in the database

## Deployment

### Prerequisites

- OutcomeOps UI deployed (`deploy_ui = true`)
- Docker available for building the container image

### Enable in Terraform

Add to your `.tfvars`:

```hcl
mcp_server_enabled = true
```

### First-Time Deployment Sequence

```bash
# 1. Create ECR repository
cd terraform
terraform plan -var-file=prd.tfvars -target=aws_ecr_repository.mcp_server -out=terraform.prd.out
terraform apply terraform.prd.out

# 2. Build and push the Docker image
ENVIRONMENT=prd make build-mcp-server-image

# 3. Deploy full infrastructure
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
terraform apply terraform.prd.out
```

### Subsequent Image Updates (code changes)

The MCP server is a **Fargate service**, not a Lambda — a code change is
not picked up by editing a function; the container image must be rebuilt
and the service redeployed, and connected clients must reconnect to pick
up any tool-schema change. The full dance:

```bash
# 1. (Only if IAM / env / task-def changed) apply terraform first.
#    e.g. the retrieval consolidation added bedrock:Rerank to the task
#    role and rerank env vars — those need an apply before the new image
#    will have permission to call the reranker.
cd terraform && terraform apply -var-file=prd.tfvars   # CI/CD in customer accounts

# 2. Rebuild + push the image (multi-region aware: also pushes to the
#    secondary ECR when secondary_region is set in the tfvars).
ENVIRONMENT=prd make build-mcp-server-image

# 3. Force a new ECS deployment so tasks pull the new :latest image.
#    Service + cluster names follow {env}-{app}-mcp-outcomeops.
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service prd-outcome-ops-ai-assist-mcp-outcomeops \
  --force-new-deployment --region us-west-2
# Repeat for the secondary region if HA is enabled.

# 4. Reconnect MCP clients. Claude Code (and other clients) cache the
#    tool list at handshake time, so a schema change (new params like
#    top_k / max_tokens / min_score) is invisible until the client
#    reconnects. Restart the client / re-add the server.
```

Verify the retrieval contract after deploy: a broad `query_knowledge_base`
question should return a bounded response (≤ ~8KB at default settings),
not the pre-fix 60KB. See ADR-023 for the contract.

## Infrastructure Details

### What Gets Deployed

| Resource | Details |
|----------|---------|
| ECR Repository | `{env}-{app}-mcp-outcomeops` |
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM |
| ALB Listener Rule | Priority 3, path `/mcp/v1` |
| Cloud Map | `outcomeops-mcp.mcp.local` |
| CloudWatch Logs | `/ecs/{env}-outcome-ops-ai-assist-mcp-outcomeops` |
| IAM Task Role | DynamoDB, S3 Vectors, Bedrock, S3 KB read, SSM (MCP tokens) |

### Security

- API keys are hashed (SHA-256) before storage
- Keys are scoped to the user who created them
- Workspace access follows existing permissions
- ALB routes MCP traffic directly (OIDC bypassed, API key auth instead)
- Container runs in private subnets
- All traffic is HTTPS

## Troubleshooting

### Connection refused / timeout

- Verify the ECS service is running: check ECS console for the `outcomeops-mcp` service
- Check ALB listener rules: `/mcp/v1` should route to the MCP target group
- Verify security groups allow ALB to reach the container on port 8080

### 401 Unauthorized

- Check that the API key is correct (starts with `ooa_`)
- Verify the key is not expired or revoked (check in the web UI)
- Ensure the `Authorization` header format is `Bearer ooa_xxxxx`

### Empty results from knowledge base

- Verify the workspace has documents ingested
- Check that your user has access to the workspace
- Try `list_workspaces` to see which workspaces you can access
- Try `list_workspace_documents` to verify documents are ingested

### Tools not showing up in MCP client

- Verify the MCP client configuration URL ends with `/mcp/v1`
- Test the connection with curl:
  ```bash
  curl -X POST https://your-domain.com/mcp/v1 \
    -H "Authorization: Bearer ooa_your_key" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
  ```

## Related Documentation

- [Getting Started](getting-started.md)
- [SonarQube MCP Server](sonarqube-mcp.md)
- [Architecture Overview](architecture.md)
