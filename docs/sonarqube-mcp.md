# SonarQube MCP Server Setup

OutcomeOps deploys the [SonarQube MCP server](https://github.com/SonarSource/sonarqube-mcp-server) as a Fargate Spot container in your VPC. This gives the AI assistant access to SonarQube code quality and security analysis tools.

## Prerequisites

- OutcomeOps UI deployed (`deploy_ui = true` with VPC configured)
- A SonarQube account (SonarCloud or self-hosted SonarQube Server)
- A SonarQube API token

## Step 1: Create a SonarQube API Token

### SonarCloud
1. Go to [sonarcloud.io](https://sonarcloud.io) and sign in
2. Navigate to **My Account** > **Security**
3. Generate a new token with **Analyze** permissions
4. Copy the token value

### Self-Hosted SonarQube
1. Sign in to your SonarQube instance
2. Navigate to **My Account** > **Security**
3. Generate a new token
4. Copy the token value

## Step 2: Store the Token in SSM Parameter Store

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

## Step 3: Enable in Terraform

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

Then deploy:

```bash
terraform plan
terraform apply
```

This will:
- Deploy the SonarQube MCP server container on Fargate Spot
- Register it in Cloud Map for internal DNS resolution
- Add the chat Lambda to the VPC (so it can reach the MCP server)
- Create a catalog entry in DynamoDB (visible in the MCP management UI)

## Step 4: Assign Access in the UI

After deployment, the SonarQube MCP server appears in **Org Settings > MCP Servers** with status `pending`.

1. Click **Refresh Tools** to fetch the available tools from the MCP server
2. Assign the server to specific workspaces, or mark it as **Global** for all users
3. The tools will appear in the chat MCP dropdown for assigned users

## What Gets Deployed

| Resource | Details |
|---|---|
| ECS Task | Fargate Spot, 0.5 vCPU, 1 GB RAM |
| Container | `mcp/sonarqube:latest` from Docker Hub |
| Network | Internal only, same VPC as UI |
| Service Discovery | Cloud Map DNS: `sonarqube-mcp.mcp.local` |
| Logs | CloudWatch: `/ecs/{env}-outcome-ops-ai-assist-mcp-sonarqube` |

## Troubleshooting

### MCP server shows "error" status after refresh
- Check CloudWatch logs for the container
- Verify the SonarQube token is valid and has sufficient permissions
- Verify `mcp_sonarqube_url` is correct and reachable from the VPC

### Chat Lambda can't reach MCP server
- Verify the ECS service is running: `aws ecs list-tasks --cluster {env}-outcome-ops-ai-assist-ui --service-name sonarqube-mcp`
- Check that Cloud Map service discovery is registered
- If using private subnets, ensure NAT gateway is configured for container image pulls

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
