# OutcomeOps MCP Server

Connect Claude Code, VS Code, Cursor, and other MCP-compatible tools to your
organization's knowledge base. Get coding standards, ADRs, and architectural
patterns directly in your development environment.

## How It Works

The OutcomeOps MCP server implements the [Model Context Protocol](https://modelcontextprotocol.io/)
Streamable HTTP transport. MCP clients connect via HTTPS, authenticate with a
personal API key, and call tools that query the knowledge base using RAG
(Retrieval-Augmented Generation).

```
Developer's Machine                     AWS (OutcomeOps)
+---------------------+                 +----------------------------+
| Claude Code / VSCode|  Streamable HTTP | ALB -> Fargate (MCP Server)|
| MCP Client          | --------------> | Auth: API Key              |
|   url: /mcp/v1      |  Bearer token   | Tools: KB Query, Standards |
+---------------------+                 +----------------------------+
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

Add to your Claude Code MCP configuration (`~/.claude/mcp.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
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

### Subsequent Image Updates

```bash
ENVIRONMENT=prd make build-mcp-server-image
# Then force a new ECS deployment if needed
```

## Infrastructure Details

### What Gets Deployed

| Resource | Details |
|----------|---------|
| ECR Repository | `{env}-{app}-mcp-outcomeops` |
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM |
| ALB Listener Rule | Priority 3, path `/mcp/v1` |
| Cloud Map | `outcomeops-mcp.mcp.local` |
| CloudWatch Logs | `/ecs/{env}-outcome-ops-ai-assist-mcp-outcomeops` |
| IAM Task Role | DynamoDB, S3 Vectors, Bedrock, S3 KB read |

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
