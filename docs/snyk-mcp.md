# Snyk MCP Server

OutcomeOps integrates with [Snyk](https://snyk.io/) via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), giving the AI assistant direct access to security scanning capabilities during chat conversations.

The integration wraps the [Snyk CLI MCP server](https://docs.snyk.io/snyk-cli/snyk-mcp-experimental) using [supergateway](https://github.com/supercorp-ai/supergateway) to bridge stdio transport to HTTP, deployed as a Fargate Spot container in your VPC.

## What This Enables

With the Snyk MCP server connected, users can ask the AI assistant questions like:

- "Scan this code for security vulnerabilities"
- "Check our dependencies for known CVEs"
- "Are there any infrastructure-as-code misconfigurations?"
- "Scan the container image for vulnerabilities"
- "Generate an SBOM for this project"

The AI assistant calls Snyk tools in real-time during the conversation, retrieves the results, and provides analysis and remediation recommendations.

## How It Works

```
User (Chat) --> Bedrock (Claude) --> Tool Call --> Lambda --> ALB --> MCP Container --> Snyk API
                                                                         |
                                                              snyk-mcp.mcp.local:8080
                                                                         |
                                                          supergateway (stdio -> HTTP)
                                                                         |
                                                            snyk mcp -t stdio
```

1. User asks a question in chat and selects the Snyk MCP server from the toolbar
2. Claude identifies which Snyk tools to call based on the question
3. The chat Lambda executes the tool call via JSON-RPC 2.0 to the MCP container
4. supergateway proxies the request to the Snyk CLI running in stdio mode
5. The Snyk CLI calls the Snyk API using the configured token
6. Results are returned to Claude, which synthesizes a response for the user

## Available Tools

The Snyk MCP server exposes these tools (fetched dynamically via **Refresh Tools** in the UI):

| Tool | Description |
|------|-------------|
| `snyk_code_scan` | Static application security testing (SAST) for proprietary code |
| `snyk_sca_scan` | Open source dependency vulnerability scanning |
| `snyk_iac_scan` | Infrastructure as Code configuration scanning |
| `snyk_container_scan` | Container image vulnerability scanning |
| `snyk_sbom_scan` | Software Bill of Materials analysis |
| `snyk_aibom` | AI Bill of Materials generation |

The tool manifest is cached in DynamoDB and refreshed on demand (configurable interval, default 7 days).

## Setup

### Prerequisites

- OutcomeOps UI deployed (`deploy_ui = true` with VPC configured)
- A Snyk account with API access
- A Snyk API token
- Docker installed (for building the container image)

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

Add to your `prd.tfvars` (or `dev.tfvars`):

```hcl
mcp_snyk_enabled = true
mcp_snyk_org     = "your-snyk-org-id"
```

| Variable | Required | Description |
|----------|----------|-------------|
| `mcp_snyk_enabled` | Yes | Set to `true` to deploy the server |
| `mcp_snyk_org` | No | Snyk organization ID (for multi-org accounts) |

### Step 4: Build and Push the Container Image

The Snyk MCP server uses a custom container image that wraps the Snyk CLI with supergateway. Build and push it to ECR:

```bash
ENVIRONMENT=prd make build-snyk-image
```

This will:
- Create the ECR repository (if it doesn't exist)
- Build the Docker image from `containers/mcp-snyk/Dockerfile`
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

The Snyk CLI MCP server only supports `stdio` and `sse` transports. SSE binds to a random port and is documented as local-only. To run it in a container behind an ALB, we use supergateway to bridge stdio to a fixed HTTP port:

```
ALB (:443/mcp/snyk) --> Container (:8080) --> supergateway --> snyk mcp -t stdio
```

The container is built from `containers/mcp-snyk/Dockerfile`:
- Base image: `node:22-slim`
- Installs `snyk` CLI and `supergateway` globally via npm
- Runs supergateway on port 8080, proxying to `snyk mcp -t stdio`
- `SNYK_TOKEN` injected as a secret from SSM at task startup

## Infrastructure Details

### What Gets Deployed

| Resource | Details |
|----------|---------|
| ECR Repository | `{env}-outcome-ops-ai-assist-mcp-snyk` |
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM (auto-restarts on Spot interruption) |
| Container | Custom image from ECR (node:22-slim + snyk CLI + supergateway) |
| Network | Internal only, same VPC as UI. ALB routes `/mcp/snyk/*` at priority 6 |
| Service Discovery | Cloud Map DNS: `snyk-mcp.mcp.local` |
| Logs | CloudWatch: `/ecs/{env}-outcome-ops-ai-assist-mcp-snyk` |
| DynamoDB | Catalog entry: `PK=MCP#catalog-snyk, SK=METADATA` |

### Security

- The MCP container runs in private subnets with no direct internet access (except via NAT gateway)
- The Snyk API token is stored in SSM Parameter Store as a `SecureString` and injected as an environment variable at task startup
- ALB listener rules for MCP traffic bypass OIDC authentication (Lambda authenticates requests internally)

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
- Check CloudWatch logs for the container:
  ```bash
  aws logs tail /ecs/prd-outcome-ops-ai-assist-mcp-snyk --follow
  ```
- Verify the Snyk token is valid: `SNYK_TOKEN=xxx snyk auth check`
- Verify `mcp_snyk_org` is correct (if using multi-org)

### Container fails to start
- Check ECS task stopped reason:
  ```bash
  aws ecs list-tasks --cluster prd-outcome-ops-ai-assist-ui --service-name snyk-mcp
  ```
- Verify the ECR image exists and was pushed successfully
- Ensure NAT gateway is configured for npm package downloads during container startup

### Tools not appearing in chat dropdown
- Confirm the MCP server status is `active` (not `pending` or `error`)
- Confirm the server is assigned to the user's workspace (or marked as Global)
- Try clicking **Refresh Tools** again in the MCP Servers admin page

### Token rotation

To rotate the Snyk token:

```bash
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/mcp/snyk-token" \
  --type SecureString \
  --value "new-token-value" \
  --overwrite
```

Then force a new ECS deployment to pick up the new value:

```bash
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service snyk-mcp \
  --force-new-deployment
```

### Updating the Snyk CLI version

To update the Snyk CLI to the latest version, rebuild and push the container:

```bash
ENVIRONMENT=prd make build-snyk-image
```

Then force a new ECS deployment:

```bash
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
