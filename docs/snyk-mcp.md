# Snyk MCP Server

OutcomeOps integrates with [Snyk](https://snyk.io/) via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), giving the AI assistant direct access to security vulnerability data during chat conversations.

The integration uses a custom MCP server that calls the [Snyk REST API](https://docs.snyk.io/snyk-api) to query existing scan results from Snyk's cloud. Unlike Snyk's official CLI-based MCP server (which requires local project files), this server queries data from projects already monitored in Snyk.

## What This Enables

With the Snyk MCP server connected, users can ask the AI assistant questions like:

- "What are the critical vulnerabilities in our payment service?"
- "List all projects being monitored in Snyk"
- "Show me the dependency graph for the API project"
- "Are there known vulnerabilities in express@4.17.1?"
- "What's our overall security posture across all projects?"
- "Generate an SBOM for the frontend project"

The AI assistant calls Snyk tools in real-time during the conversation, retrieves the results, and provides analysis and remediation recommendations based on actual scan data.

## How It Works

```
User (Chat) --> Bedrock (Claude) --> Tool Call --> Lambda --> ALB --> MCP Container --> Snyk REST API
                                                                         |
                                                              snyk-mcp.mcp.local:8080
                                                                         |
                                                              server.py (JSON-RPC 2.0)
                                                                         |
                                                          api.snyk.io / snyk.io/api/v1
```

1. User asks a question in chat and selects the Snyk MCP server from the toolbar
2. Claude identifies which Snyk tools to call based on the question
3. The chat Lambda executes the tool call via JSON-RPC 2.0 to the MCP container
4. The container calls the Snyk REST API using the configured token
5. Results are returned to Claude, which synthesizes a response for the user

## Available Tools

| Tool | Description |
|------|-------------|
| `list_organizations` | List all Snyk organizations the user belongs to |
| `list_projects` | List all monitored projects in an organization |
| `get_project_issues` | Get aggregated vulnerabilities for a project (with severity/type filters) |
| `get_project_dependencies` | Get the dependency graph for a project |
| `search_issues_by_package` | Search for vulnerabilities in a specific package by purl |
| `get_org_issues` | List all issues across an organization (with severity/scan type filters) |
| `get_project_sbom` | Export SBOM in CycloneDX or SPDX format |

The tool manifest is cached in DynamoDB and refreshed on demand (configurable interval, default 7 days).

## Setup

### Prerequisites

- OutcomeOps UI deployed (`deploy_ui = true` with VPC configured)
- A **paid Snyk plan** (Team or Enterprise) -- the free tier does not include REST API access
- A Snyk API token
- Docker installed (for building the container image)
- At least one project monitored in Snyk (the tools query existing scan results)

> **Important:** Snyk's free tier blocks REST API access at the organization level. A valid API token alone is not sufficient -- the organization must be on a paid plan that includes API entitlements. If you see `"not entitled for api access"` errors, this is a plan limitation, not a token or configuration issue.

### Step 1: Create a Snyk API Token

1. Sign in to [app.snyk.io](https://app.snyk.io)
2. Navigate to **Settings** > **General** > **Auth Token** (or **Service accounts** for CI tokens)
3. Copy the token value

### Step 2: Store the Token in SSM Parameter Store

```bash
# For production
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/mcp/snyk-token" \
  --type SecureString \
  --value "your-snyk-token-here"

# For dev
aws ssm put-parameter \
  --name "/dev/outcome-ops-ai-assist/mcp/snyk-token" \
  --type SecureString \
  --value "your-snyk-token-here"
```

### Step 3: Enable in Terraform

Add to your `prd.tfvars`:

```hcl
mcp_snyk_enabled = true
mcp_snyk_org     = "your-snyk-org-slug"
```

| Variable | Required | Description |
|----------|----------|-------------|
| `mcp_snyk_enabled` | Yes | Set to `true` to deploy the server |
| `mcp_snyk_org` | No | Snyk organization slug or ID (for multi-org accounts). If omitted, uses the first org. |

### Step 4: Build and Push the Container Image

```bash
ENVIRONMENT=prd make build-snyk-image
```

This will:
- Create the ECR repository (if it doesn't exist)
- Build the Docker image from `containers/mcp-snyk/`
- Push to ECR

### Step 5: Deploy Infrastructure

```bash
cd terraform
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
terraform apply terraform.prd.out
```

This will:
- Deploy the Snyk MCP server container on Fargate Spot
- Register it in Cloud Map for internal DNS resolution (`snyk-mcp.mcp.local`)
- Add an ALB listener rule to route `/mcp/snyk/*` traffic to the container
- Create a catalog entry in DynamoDB (visible in the MCP management UI)

### Step 6: Activate and Assign Access in the UI

After deployment, the Snyk MCP server appears in **Org Settings > MCP Servers** with status `pending`.

1. Click **Refresh Tools** to fetch the available tools from the MCP server
2. Status changes from `pending` to `active` and the tool count is displayed
3. Assign the server to specific workspaces, or mark it as **Global** for all users
4. The Snyk tools will appear in the chat MCP dropdown for assigned users

## Container Architecture

The container is a lightweight Python HTTP server that implements the MCP Streamable HTTP transport (JSON-RPC 2.0 over HTTP POST). It calls the Snyk REST API directly -- no Snyk CLI required.

```
ALB (:443/mcp/snyk) --> Container (:8080) --> server.py --> Snyk REST API
```

Built from `containers/mcp-snyk/`:
- Base image: `python:3.12-slim`
- No external dependencies (uses stdlib `urllib`, `http.server`)
- `SNYK_TOKEN` injected as a secret from SSM at task startup
- `SNYK_CFG_ORG` set as an environment variable for org scoping

## Infrastructure Details

### What Gets Deployed

| Resource | Details |
|----------|---------|
| ECR Repository | `{env}-outcome-ops-ai-assist-mcp-snyk` |
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM (auto-restarts on Spot interruption) |
| Container | Custom image from ECR (python:3.12-slim + server.py) |
| Network | Internal only, same VPC as UI. ALB routes `/mcp/snyk/*` at priority 6 |
| Service Discovery | Cloud Map DNS: `snyk-mcp.mcp.local` |
| Logs | CloudWatch: `/ecs/{env}-outcome-ops-ai-assist-mcp-snyk` |
| DynamoDB | Catalog entry: `PK=MCP#catalog-snyk, SK=METADATA` |

### Security

- The MCP container runs in private subnets
- The Snyk API token is stored in SSM Parameter Store as a `SecureString` and injected as an environment variable at task startup
- ALB listener rules for MCP traffic bypass OIDC authentication (Lambda authenticates requests internally)
- The container makes outbound HTTPS calls to `api.snyk.io` and `snyk.io` (requires NAT gateway or public subnet)

### Cost

Fargate Spot pricing applies. Typical cost for a single MCP server running 24/7:

| Resource | Estimated Monthly Cost |
|----------|----------------------|
| Fargate Spot (0.5 vCPU, 1 GB) | ~$5-8 |
| Cloud Map | ~$0.50 |
| CloudWatch Logs | ~$0.50 |
| ECR Storage | ~$0.10 |
| **Total** | **~$6-9/month** |

## Troubleshooting

### MCP server shows "error" status after refresh
- Check CloudWatch logs:
  ```bash
  aws logs tail /ecs/prd-outcome-ops-ai-assist-mcp-snyk --follow
  ```
- Verify the Snyk token is valid
- Verify the org slug matches your Snyk account

### Container fails to start
- Check ECS task stopped reason
- Verify the ECR image exists and was pushed successfully
- Ensure NAT gateway is configured (the container calls `api.snyk.io`)

### "No Snyk organizations found"
- Verify `SNYK_TOKEN` has access to at least one org
- Set `mcp_snyk_org` explicitly in tfvars

### Tools not appearing in chat dropdown
- Confirm the MCP server status is `active` (not `pending` or `error`)
- Confirm the server is assigned to the user's workspace (or marked as Global)
- Try clicking **Refresh Tools** again

### Token rotation

```bash
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/mcp/snyk-token" \
  --type SecureString \
  --value "new-token-value" \
  --overwrite
```

Then force a new ECS deployment:

```bash
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service snyk-mcp \
  --force-new-deployment
```

### Updating the server

Rebuild and push:

```bash
ENVIRONMENT=prd make build-snyk-image
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service snyk-mcp \
  --force-new-deployment
```

## Related Documentation

- [SonarQube MCP Server](sonarqube-mcp.md) - Similar integration for code quality
- [Getting Started](getting-started.md) - Initial deployment setup
- [Architecture Overview](architecture.md) - System design and data flows
- [Deployment Guide](deployment-guide.md) - Operations, cost estimation, and credential rotation
