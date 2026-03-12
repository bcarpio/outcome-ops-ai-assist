# SonarQube MCP Server

OutcomeOps integrates with [SonarQube](https://www.sonarsource.com/products/sonarqube/) via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), giving the AI assistant direct access to code quality and security analysis data during chat conversations.

The integration uses the [official SonarQube MCP server](https://github.com/SonarSource/sonarqube-mcp-server) from SonarSource, deployed as a Fargate Spot container in your VPC.

## What This Enables

With the SonarQube MCP server connected, users can ask the AI assistant questions like:

- "What are the critical security vulnerabilities in our payment service?"
- "Show me the code smells in the authentication module"
- "What's our overall code quality gate status?"
- "List all open bugs with severity higher than major"
- "What security hotspots need review?"

The AI assistant calls SonarQube tools in real-time during the conversation, retrieves the results, and provides analysis and recommendations based on actual project data.

## How It Works

```
User (Chat) --> Bedrock (Claude) --> Tool Call --> Lambda --> ALB --> MCP Container --> SonarQube API
                                                                         |
                                                              sonarqube-mcp.mcp.local:8080
```

1. User asks a question in chat and selects the SonarQube MCP server from the toolbar
2. Claude identifies which SonarQube tools to call based on the question
3. The chat Lambda executes the tool call via JSON-RPC 2.0 to the MCP container
4. The MCP container calls the SonarQube API using the configured token
5. Results are returned to Claude, which synthesizes a response for the user

MCP tools can be used alongside knowledge base workspaces. For example, a user can select both a workspace (for ADR/pattern context) and the SonarQube MCP server (for live quality data) in the same conversation.

## Available Tools

The SonarQube MCP server exposes tools from the official SonarSource implementation. The exact tool list is fetched dynamically when you click **Refresh Tools** in the UI. Typical tools include:

| Tool | Description |
|------|-------------|
| `sonarqube_issues` | Search for issues (bugs, vulnerabilities, code smells) with filters |
| `sonarqube_metrics` | Get project metrics (coverage, duplications, complexity) |
| `sonarqube_projects` | List and search projects |
| `sonarqube_quality_gate` | Check quality gate status for a project |
| `sonarqube_hotspots` | List security hotspots requiring review |
| `sonarqube_source` | View source code with issue annotations |
| `sonarqube_rules` | Look up coding rules and their descriptions |

The tool manifest is cached in DynamoDB and refreshed on demand (configurable interval, default 7 days).

## Supported SonarQube Versions

| Platform | URL Format | Notes |
|----------|------------|-------|
| **SonarCloud** | `https://sonarcloud.io` | Requires `mcp_sonarqube_org` |
| **SonarQube Server** (self-hosted) | `https://sonarqube.your-company.com` | `mcp_sonarqube_org` not needed |

## Setup

### Prerequisites

- OutcomeOps UI deployed (`deploy_ui = true` with VPC configured)
- A SonarQube account (SonarCloud or self-hosted)
- A SonarQube API token with read access to your projects

### Step 1: Create a SonarQube API Token

#### SonarCloud
1. Go to [sonarcloud.io](https://sonarcloud.io) and sign in
2. Navigate to **My Account** > **Security**
3. Generate a new token with **Analyze** permissions
4. Copy the token value

#### Self-Hosted SonarQube
1. Sign in to your SonarQube instance
2. Navigate to **My Account** > **Security**
3. Generate a new token (type: User Token recommended)
4. Copy the token value

### Step 2: Store the Token in SSM Parameter Store

The token is stored in SSM at a well-known path. OutcomeOps automatically looks for it here.

```bash
# For production
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/mcp/sonarqube-token" \
  --type SecureString \
  --value "your-sonarqube-token-here"

# For dev
aws ssm put-parameter \
  --name "/dev/outcome-ops-ai-assist/mcp/sonarqube-token" \
  --type SecureString \
  --value "your-sonarqube-token-here"
```

### Step 3: Enable in Terraform

Add to your `prd.tfvars` (or `dev.tfvars`):

```hcl
# SonarCloud
mcp_sonarqube_enabled = true
mcp_sonarqube_url     = "https://sonarcloud.io"
mcp_sonarqube_org     = "your-organization-key"

# OR self-hosted SonarQube
mcp_sonarqube_enabled = true
mcp_sonarqube_url     = "https://sonarqube.internal.company.com"
```

| Variable | Required | Description |
|----------|----------|-------------|
| `mcp_sonarqube_enabled` | Yes | Set to `true` to deploy the server |
| `mcp_sonarqube_url` | Yes | SonarQube instance URL |
| `mcp_sonarqube_org` | SonarCloud only | Organization key (found in SonarCloud project settings) |

Then deploy:

```bash
cd terraform
terraform plan -var-file=prd.tfvars -out=terraform.prd.out
terraform apply terraform.prd.out
```

This will:
- Deploy the SonarQube MCP server container on Fargate Spot
- Register it in Cloud Map for internal DNS resolution (`sonarqube-mcp.mcp.local`)
- Add an ALB listener rule to route `/mcp/*` traffic to the container
- Create a catalog entry in DynamoDB (visible in the MCP management UI)

### Step 4: Activate and Assign Access in the UI

After deployment, the SonarQube MCP server appears in **Org Settings > MCP Servers** with status `pending`.

1. Click **Refresh Tools** to fetch the available tools from the MCP server
2. Status changes from `pending` to `active` and the tool count is displayed
3. Assign the server to specific workspaces, or mark it as **Global** for all users
4. The SonarQube tools will appear in the chat MCP dropdown for assigned users

## Infrastructure Details

### What Gets Deployed

| Resource | Details |
|----------|---------|
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM (auto-restarts on Spot interruption) |
| Container | `mcp/sonarqube:latest` from Docker Hub (official SonarSource image) |
| Network | Internal only, same VPC as UI. ALB routes `/mcp/*` at priority 5 |
| Service Discovery | Cloud Map DNS: `sonarqube-mcp.mcp.local` |
| Logs | CloudWatch: `/ecs/{env}-outcome-ops-ai-assist-mcp-sonarqube` |
| DynamoDB | Catalog entry: `PK=MCP#catalog-sonarqube, SK=METADATA` |

### Security

- The MCP container runs in private subnets with no direct internet access
- The SonarQube API token is stored in SSM Parameter Store as a `SecureString` and injected as an environment variable at task startup
- ALB listener rules for MCP traffic bypass OIDC authentication (Lambda authenticates requests internally)
- Credentials for custom (non-catalog) MCP servers are encrypted at rest in DynamoDB via KMS

### Cost

Fargate Spot pricing applies. Typical cost for a single MCP server running 24/7:

| Resource | Estimated Monthly Cost |
|----------|----------------------|
| Fargate Spot (0.5 vCPU, 1 GB) | ~$5-8 |
| Cloud Map | ~$0.50 |
| CloudWatch Logs | ~$0.50 |
| **Total** | **~$6-9/month** |

## Adding Custom MCP Servers

Every MCP server requires Terraform infrastructure (ECS task, ALB routing, Cloud Map, DynamoDB catalog entry) before it appears in the UI. The pattern is the same as SonarQube:

1. **Add Terraform config** -- use the `mcp-server` module in `terraform/mcp.tf`:
   - Container image (from Docker Hub or a custom ECR build)
   - Environment variables and secrets (SSM)
   - ALB listener rule priority and path pattern
   - Cloud Map service discovery registration
   - DynamoDB catalog entry (`is_catalog = true`)

2. **Deploy infrastructure** -- `terraform plan` / `terraform apply`

3. **Assign to workspaces via the UI** -- the server appears in **Org Settings > MCP Servers** after deploy. Org admins assign it to workspaces and click **Refresh Tools** to fetch the tool manifest.

See `terraform/mcp.tf` for the SonarQube example to follow as a template.

## Troubleshooting

### MCP server shows "error" status after refresh
- Check CloudWatch logs for the container:
  ```bash
  aws logs tail /ecs/prd-outcome-ops-ai-assist-mcp-sonarqube --follow
  ```
- Verify the SonarQube token is valid and has sufficient permissions
- Verify `mcp_sonarqube_url` is correct and reachable from the VPC

### Chat Lambda can't reach MCP server
- Verify the ECS service is running:
  ```bash
  aws ecs list-tasks \
    --cluster prd-outcome-ops-ai-assist-ui \
    --service-name sonarqube-mcp
  ```
- Check that Cloud Map service discovery is registered
- If using private subnets, ensure NAT gateway is configured for container image pulls

### Tools not appearing in chat dropdown
- Confirm the MCP server status is `active` (not `pending` or `error`)
- Confirm the server is assigned to the user's workspace (or marked as Global)
- Try clicking **Refresh Tools** again in the MCP Servers admin page

### Token rotation

To rotate the SonarQube token:

```bash
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/mcp/sonarqube-token" \
  --type SecureString \
  --value "new-token-value" \
  --overwrite
```

Then force a new ECS deployment to pick up the new value:

```bash
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service sonarqube-mcp \
  --force-new-deployment
```

## Related Documentation

- [Getting Started](getting-started.md) - Initial deployment setup
- [Architecture Overview](architecture.md) - System design and data flows
- [Deployment Guide](deployment-guide.md) - Operations, cost estimation, and credential rotation
